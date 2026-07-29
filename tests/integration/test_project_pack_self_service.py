"""Integrationstest: Self-Service-Projekt-Paket (Mollie one-off → manual_project_credits).
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
from src.billing.project_credits_service import get_available_credits


async def seed_user(conn, *, returning: bool):
    tid = "t_" + uuid.uuid4().hex[:10]
    await conn.execute(
        """INSERT INTO tenants (id, name, account_type, billing_name, billing_street,
             billing_city, billing_postcode, billing_country)
           VALUES ($1,$2,'customer','Test GmbH','Teststrasse 1','Wien','1010','AT')""",
        tid, "Test GmbH",
    )
    uid = await conn.fetchval(
        "INSERT INTO users (email, name, tenant_id, role) VALUES ($1,$2,$3,'owner') RETURNING id",
        f"u-{uuid.uuid4().hex[:8]}@test.local", "Test Owner", tid,
    )
    if returning:
        inv = await conn.fetchval(
            """INSERT INTO invoices (invoice_number, user_id, tenant_id, subtotal_eur, tax_eur, total_eur, status)
               VALUES ($1,$2,$3,1000,200,1200,'paid') RETURNING id""",
            f"INV-{uuid.uuid4().hex[:8]}", uid, tid,
        )
        await conn.execute(
            """INSERT INTO pending_orders (user_id, tenant_id, plan_id, quantity, total_price_eur, status, invoice_id)
               VALUES ($1,$2,'energy-project',1,1000,'released',$3)""",
            uid, tid, inv,
        )
    return str(uid), tid


async def main():
    await init_pool()
    await reload_plans()
    pool = get_pool()
    results = []

    # --- TEST A: Bestandskunde kauft 3er-Paket self-service ---
    async with pool.acquire() as conn:
        uid, tid = await seed_user(conn, returning=True)
    checkout = await billing_service.start_project_pack_checkout(
        uid, "energy-project", 3, "https://x/ok", "u@test.local", "Test")
    pay_id = checkout["paymentId"]
    res = await billing_service.handle_webhook(pay_id)
    avail = await get_available_credits(uuid.UUID(uid), "energy-project")
    results.append(("A: Kauf gewährt 3 Credits", avail == 3 and res.get("handled"),
                    f"avail={avail} webhook={res.get('handled')}/{res.get('type')}"))
    async with pool.acquire() as conn:
        paid = await conn.fetchval(
            """SELECT count(*) FROM invoices i JOIN pending_orders o ON o.invoice_id=i.id
               WHERE o.user_id=$1 AND o.payment_method='mollie' AND i.status='paid' AND o.status='released'""",
            uuid.UUID(uid))
    results.append(("A: Mollie-Order Invoice 'paid' + Order 'released'", paid == 1, f"matched={paid}"))

    # --- TEST A2: energy-project (NICHT report-check-credit) darf NIE auto-approved
    # werden — nur der WerkING-Check hat die Auto-Invoice-Lane (Rafael 2026-07-29). ---
    async with pool.acquire() as conn:
        not_approved = await conn.fetchval(
            """SELECT count(*) FROM invoices i JOIN pending_orders o ON o.invoice_id=i.id
               WHERE o.user_id=$1 AND o.plan_id='energy-project' AND i.approved_at IS NOT NULL""",
            uuid.UUID(uid))
    results.append(("A2: energy-project-Rechnung bleibt manuell (approved_at NULL)",
                    not_approved == 0, f"unexpectedly_approved={not_approved}"))

    # --- TEST B: Webhook-Retry idempotent (kein Doppel-Grant) ---
    res2 = await billing_service.handle_webhook(pay_id)
    avail2 = await get_available_credits(uuid.UUID(uid), "energy-project")
    results.append(("B: Retry idempotent (weiterhin 3)", avail2 == 3,
                    f"avail={avail2} idempotent={res2.get('idempotent')}"))

    # --- TEST C: Neukunde (keine released order) wird geblockt ---
    async with pool.acquire() as conn:
        uid2, _ = await seed_user(conn, returning=False)
    blocked = False; detail = "kein Fehler!"
    try:
        await billing_service.start_project_pack_checkout(
            uid2, "energy-project", 1, "https://x/ok", "u2@test.local", "Test2")
    except ValueError as e:
        blocked = "returning customers" in str(e); detail = str(e)[:70]
    results.append(("C: Neukunde geblockt (Bestandskunden-Gate)", blocked, detail))

    # --- TEST D: Nicht-Projekt-Plan abgelehnt ---
    rejected = False; d2 = "kein Fehler!"
    try:
        await billing_service.start_project_pack_checkout(
            uid, "report-standard", 1, "https://x/ok", "u@test.local", "Test")
    except ValueError as e:
        rejected = "not a project plan" in str(e); d2 = str(e)[:70]
    results.append(("D: report-standard (month) abgelehnt", rejected, d2))

    # --- TEST E: bezahlte Zahlung für stornierte Order → fail loud (defensiv) ---
    # Kein stilles Schlucken: Geld erhalten, aber Order nicht mehr freigebbar.
    async with pool.acquire() as conn:
        uid3, _ = await seed_user(conn, returning=True)
    co = await billing_service.start_project_pack_checkout(
        uid3, "energy-project", 2, "https://x/ok", "u3@test.local", "Test3")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE pending_orders SET status='cancelled' "
            "WHERE user_id=$1 AND payment_method='mollie' AND status='awaiting_payment'",
            uuid.UUID(uid3))
    failed_loud = False; d3 = "kein Fehler!"
    try:
        await billing_service.handle_webhook(co["paymentId"])
    except RuntimeError as e:
        failed_loud = "non-releasable" in str(e); d3 = str(e)[:70]
    avail3 = await get_available_credits(uuid.UUID(uid3), "energy-project")
    results.append(("E: bezahlt+storniert → fail loud, KEINE Credits",
                    failed_loud and avail3 == 0, f"loud={failed_loud} avail={avail3}"))

    await close_pool()
    print("\n=== ERGEBNISSE ===")
    ok = True
    for name, passed, det in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {name} — {det}")
        ok = ok and bool(passed)
    print("\n" + ("✅ ALLE TESTS PASS" if ok else "❌ FEHLER"))
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
