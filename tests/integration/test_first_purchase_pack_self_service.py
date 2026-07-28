"""Integrationstest: Self-Service-ERSTKAUF-Checkout (WerkING-Check Credit-Pack).
Läuft gegen die lokale bridge_test-DB mit FakeMollie (sofort 'paid')."""
import os, asyncio, uuid

os.environ.setdefault("BRIDGE_DB_URL", os.environ.get("BRIDGE_TEST_DB_URL", "postgresql://bridge:bridge@localhost:5433/bridge_test"))
os.environ["BRIDGE_JWT_SECRET"] = "test-secret-for-integration"
os.environ["BRIDGE_SERVICE_TOKEN"] = "test-service-token"
os.environ["BRIDGE_PUBLIC_URL"] = "https://test.bridge.local"
os.environ["BRIDGE_USE_FAKE_MOLLIE"] = "true"

from src.db import init_pool, get_pool, close_pool
from src.budget.plans import reload_plans
from src.billing import billing_service
from src.billing.project_credits_service import get_available_credits, consume_credit, CreditsExhaustedError


async def seed_user(conn, *, with_billing_address: bool):
    tid = "t_" + uuid.uuid4().hex[:10]
    if with_billing_address:
        await conn.execute(
            """INSERT INTO tenants (id, name, account_type, billing_name, billing_street,
                 billing_city, billing_postcode, billing_country)
               VALUES ($1,$2,'customer','Test GmbH','Teststrasse 1','Wien','1010','AT')""",
            tid, "Test GmbH",
        )
    else:
        # Frischer Erstkäufer-Tenant OHNE Rechnungsadresse — genau der Fall,
        # den start_project_pack_checkout (Adress-Gate) ablehnen würde.
        await conn.execute(
            "INSERT INTO tenants (id, name, account_type) VALUES ($1,$2,'customer')",
            tid, "Test GmbH (no address)",
        )
    uid = await conn.fetchval(
        "INSERT INTO users (email, name, tenant_id, role) VALUES ($1,$2,$3,'owner') RETURNING id",
        f"u-{uuid.uuid4().hex[:8]}@test.local", "Test Owner", tid,
    )
    return str(uid), tid


async def main():
    await init_pool()
    await reload_plans()
    pool = get_pool()
    results = []

    # --- TEST A: Erstkäufer OHNE Rechnungsadresse + OHNE released order kauft ---
    async with pool.acquire() as conn:
        uid, tid = await seed_user(conn, with_billing_address=False)
    checkout = await billing_service.start_first_purchase_pack_checkout(
        uid, "report-check-credit", 20, "https://x/ok", "u@test.local", "Test")
    pay_id = checkout["paymentId"]
    res = await billing_service.handle_webhook(pay_id)
    avail = await get_available_credits(uuid.UUID(uid), "report-check-credit")
    results.append(("A: Erstkauf ohne Adresse/Bestandskunden-Status gewährt 20 Credits",
                    avail == 20 and res.get("handled"),
                    f"avail={avail} webhook={res.get('handled')}/{res.get('type')}"))

    # --- TEST B: Consume-Endpoint (Python-Funktion, wie von der HTTP-Route genutzt) ---
    consumed = await consume_credit(uuid.UUID(uid), "report-check-credit")
    avail_after_consume = await get_available_credits(uuid.UUID(uid), "report-check-credit")
    results.append(("B: consume_credit ohne project_id degradiert sauber (Slot-only)",
                    avail_after_consume == 19 and consumed.get("planId") == "report-check-credit",
                    f"avail={avail_after_consume} credit={consumed.get('creditId')}"))

    # --- TEST C: Webhook-Retry idempotent (kein Doppel-Grant) ---
    res2 = await billing_service.handle_webhook(pay_id)
    avail2 = await get_available_credits(uuid.UUID(uid), "report-check-credit")
    results.append(("C: Retry idempotent (weiterhin 19 nach dem einen consume)",
                    avail2 == 19, f"avail={avail2} idempotent={res2.get('idempotent')}"))

    # --- TEST D: Plan nicht in der Allowlist (z.B. energy-project) wird abgelehnt ---
    async with pool.acquire() as conn:
        uid2, _ = await seed_user(conn, with_billing_address=False)
    rejected = False; d1 = "kein Fehler!"
    try:
        await billing_service.start_first_purchase_pack_checkout(
            uid2, "energy-project", 1, "https://x/ok", "u2@test.local", "Test2")
    except ValueError as e:
        rejected = "not allowlisted" in str(e); d1 = str(e)[:80]
    results.append(("D: energy-project nicht allowlisted -> abgelehnt", rejected, d1))

    # --- TEST E: Preis-Cap-Guard greift bei zu hoher quantity ---
    capped = False; d2 = "kein Fehler!"
    try:
        # 5 EUR * 100 = 500 EUR > FIRST_PURCHASE_PACK_MAX_AMOUNT_EUR (400)
        await billing_service.start_first_purchase_pack_checkout(
            uid2, "report-check-credit", 100, "https://x/ok", "u2@test.local", "Test2")
    except ValueError as e:
        capped = "exceeds the first-purchase pack cap" in str(e); d2 = str(e)[:80]
    results.append(("E: Preis-Cap-Guard blockt Grossbestellung", capped, d2))

    # --- TEST F: Credits erschöpft -> CreditsExhaustedError (fail loud) ---
    async with pool.acquire() as conn:
        uid3, _ = await seed_user(conn, with_billing_address=False)
    co3 = await billing_service.start_first_purchase_pack_checkout(
        uid3, "report-check-credit", 1, "https://x/ok", "u3@test.local", "Test3")
    await billing_service.handle_webhook(co3["paymentId"])
    await consume_credit(uuid.UUID(uid3), "report-check-credit")  # verbraucht den einzigen Credit
    exhausted = False; d3 = "kein Fehler!"
    try:
        await consume_credit(uuid.UUID(uid3), "report-check-credit")
    except CreditsExhaustedError as e:
        exhausted = True; d3 = str(e)[:80]
    results.append(("F: Credits erschöpft -> fail loud (kein stiller Download)", exhausted, d3))

    # --- TEST G: bestehende Lane (start_project_pack_checkout) bleibt unangetastet ---
    # Erstkäufer ohne released order wird dort weiterhin geblockt.
    async with pool.acquire() as conn:
        uid4, _ = await seed_user(conn, with_billing_address=True)
    still_blocked = False; d4 = "kein Fehler!"
    try:
        await billing_service.start_project_pack_checkout(
            uid4, "energy-project", 1, "https://x/ok", "u4@test.local", "Test4")
    except ValueError as e:
        still_blocked = "returning customers" in str(e); d4 = str(e)[:80]
    results.append(("G: start_project_pack_checkout unverändert (Bestandskunden-Gate aktiv)",
                    still_blocked, d4))

    await close_pool()
    print("\n=== ERGEBNISSE ===")
    ok = True
    for name, passed, det in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {name} — {det}")
        ok = ok and bool(passed)
    print("\n" + ("✅ ALLE TESTS PASS" if ok else "❌ FEHLER"))
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
