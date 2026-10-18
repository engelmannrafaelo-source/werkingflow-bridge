"""Abo-Invariante je Abrechnungskonto (Migration 070) — gegen ECHTEN Postgres.

Codex-Pruefung 18.10.2026 zu 03c938e (Planwechsel), vier P1-Befunde, hier als
feste Proben:
1. zwei offene Wechsel -> zwei neue Abos           -> zweiter Wechsel 409
2. dauerhafter Kuendigungsfehler -> alt+neu aktiv   -> 'doppelt_aktiv' sichtbar,
                                                       Alarm, Nachholauftrag
3. Retry nach Kuendigung des Ersatzes kuendigt das letzte Abo -> altes bleibt
4. gleichzeitige identische Webhooks -> externes Geisterabo   -> genau ein Abo,
                                                       auch mit zwei Verbindungen
Dazu die Invariante selbst auf Tabellenebene (Indizes, Trigger, Migrations-
Abbruch bei Bestandsdubletten).

FakeMollie, kein Live-Aufruf. Braucht `BRIDGE_TEST_MIGRATED_PG_URL` (Rezept in
test_energy_credit_arten_echte_db.py); ohne die Variable ausdruecklich
uebersprungen. Nie gegen eine geteilte oder produktive Datenbank richten.
"""
from __future__ import annotations

import asyncio
import pathlib
import uuid
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from tests.billing.test_energy_credit_arten_echte_db import (  # noqa: F401 — Fixture db
    PG_URL,
    _consent,
    db,
)
from tests.billing.test_planwechsel_echte_db import (
    _aktive,
    _kunde_mit_aktivem_abo,
    _mollie,
    _status,
)

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="BRIDGE_TEST_MIGRATED_PG_URL fehlt — Abo-Invariante nicht gegen echten Postgres geprueft",
)

MIGRATION_070 = (pathlib.Path(__file__).resolve().parents[2]
                 / "docker" / "migrations" / "070_abo_abschluss_invariante.sql")


async def _wechsel(uid, plan):
    from src.billing import billing_service

    return await billing_service.change_subscription(
        str(uid), plan[0], plan[1], "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )


async def _abschluss(db, payment_id):
    async with db.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM abo_abschluesse WHERE payment_id=$1", payment_id)


async def _events(db, uid, typ):
    async with db.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM billing_events WHERE user_id=$1 AND event_type=$2", uid, typ,
        )


def _mollie_abos_zu(first_payment_id):
    return [k for k, v in _mollie()._subscriptions.items()
            if v["metadata"].get("firstPaymentId") == first_payment_id]


def _mollie_status(db_sub_mollie_id):
    return _mollie()._subscriptions[db_sub_mollie_id]["status"]


async def _mollie_id(db, sub_id):
    async with db.acquire() as conn:
        return await conn.fetchval(
            "SELECT mollie_subscription_id FROM subscriptions WHERE id=$1", uuid.UUID(str(sub_id)))


@pytest.fixture(autouse=True)
def _frischer_mollie():
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    yield
    reset_mollie_adapter()


# ---------------------------------------------------------------------------
# Befund 1 — hoechstens ein offener Wechsel je Konto
# ---------------------------------------------------------------------------

async def test_zweiter_offener_wechsel_wird_abgewiesen(db):
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    erster = await _wechsel(uid, plan_b)
    # Die Erstzahlung ist bei Mollie noch offen.
    offen = {**_mollie()._payments[erster["paymentId"]], "status": "open"}
    with patch.object(_mollie(), "get_payment", AsyncMock(return_value=offen)):
        with pytest.raises(billing_service.AboAbschlussOffen) as exc:
            await _wechsel(uid, (plan_a[0], 3))
    assert str(exc.value) == billing_service.ABSCHLUSS_OFFEN_SATZ
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM pending_payments WHERE user_id=$1 AND type='subscription_first'", uid,
        ) == 2  # Erstkauf + EIN Wechsel — der zweite hat bei Mollie nichts angelegt

    # Bezahlt -> genau ein neues Abo, altes gekuendigt.
    await billing_service.handle_webhook(erster["paymentId"])
    assert await _aktive(db, uid) == [plan_b]
    assert await _status(db, alt) == "cancelled"


@pytest.mark.parametrize("reihenfolge", ["wechsel_zuerst", "altzahlung_zuerst"])
async def test_codex_probe_zwei_bezahlte_wechsel_nie_zwei_abos(db, reihenfolge, caplog):
    """Codex-Reproduktion: A aktiv, zwei bezahlte Wechsel-Erstzahlungen fuer A.
    Ueber die Route entsteht die zweite gar nicht (Test oben); hier liegt sie als
    Altbestand von vor 070 vor (pending_payments ohne Abschluss). In beiden
    Webhook-Reihenfolgen: genau EIN neues Abo, die andere Zahlung -> 'konflikt'
    + Alarm, kein zweites Mollie-Abo."""
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    b = await _wechsel(uid, plan_b)
    # Altbestand: zweite Erstzahlung, angelegt vom Code vor 070.
    cust = _mollie()._payments[b["paymentId"]]["customer_id"]
    c = _mollie()._create_payment(cust, 30.0, {"type": "subscription_first", "seats": "3"})
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO pending_payments (payment_id, user_id, type, plan_id, amount_eur, ersetzt_abo_id)
               VALUES ($1, $2, 'subscription_first', $3, 30, $4)""",
            c["paymentId"], uid, plan_a[0], uuid.UUID(alt),
        )
    zahlungen = [b["paymentId"], c["paymentId"]]
    if reihenfolge == "altzahlung_zuerst":
        zahlungen.reverse()
    ausgang = {}
    for pid in zahlungen:
        try:
            await billing_service.handle_webhook(pid)
            ausgang[pid] = "aktiviert"
        except billing_service.AboAbschlussKonflikt:
            ausgang[pid] = "konflikt"
    assert sorted(ausgang.values()) == ["aktiviert", "konflikt"], ausgang
    verlierer = next(p for p, a in ausgang.items() if a == "konflikt")
    with pytest.raises(billing_service.AboAbschlussKonflikt):
        await billing_service.handle_webhook(verlierer)              # Wiederholung: weiter laut
    assert len(await _aktive(db, uid)) == 1
    assert await _status(db, alt) == "cancelled"
    assert (await _abschluss(db, verlierer))["status"] == "konflikt"
    assert _mollie_abos_zu(verlierer) == []
    assert await _events(db, uid, "subscription.activation_conflict") == 1
    assert any("[ALARM][abo-abschluss]" in r.getMessage() for r in caplog.records)


async def test_gescheiterte_erstzahlung_gibt_platz_frei(db):
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    erster = await _wechsel(uid, plan_b)
    gescheitert = {**_mollie()._payments[erster["paymentId"]], "status": "expired"}
    with patch.object(_mollie(), "get_payment", AsyncMock(return_value=gescheitert)):
        await billing_service.handle_webhook(erster["paymentId"])
    assert (await _abschluss(db, erster["paymentId"]))["status"] == "abgebrochen"
    zweiter = await _wechsel(uid, (plan_a[0], 3))
    await billing_service.handle_webhook(zweiter["paymentId"])
    assert await _aktive(db, uid) == [(plan_a[0], 3)]


async def test_verlorener_webhook_mollie_sagt_gescheitert_platz_frei(db):
    """Kam der Fehl-Webhook nie an, klaert der naechste Wechsel den Platz bei Mollie."""
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    erster = await _wechsel(uid, plan_b)
    gescheitert = {**_mollie()._payments[erster["paymentId"]], "status": "canceled"}
    with patch.object(_mollie(), "get_payment", AsyncMock(return_value=gescheitert)):
        zweiter = await _wechsel(uid, (plan_a[0], 3))
    assert (await _abschluss(db, erster["paymentId"]))["status"] == "abgebrochen"
    assert (await _abschluss(db, zweiter["paymentId"]))["status"] == "offen"


async def test_liegengebliebene_reservierung_verfaellt(db):
    """Absturz zwischen Reservierung und Checkout: nach dem Verfall frei, vorher 409."""
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    with patch.object(_mollie(), "create_first_payment", AsyncMock(side_effect=KeyboardInterrupt)), \
         patch("src.billing.billing_service._abschluss_abbrechen", AsyncMock()):
        with pytest.raises(KeyboardInterrupt):
            await _wechsel(uid, plan_b)
    with pytest.raises(billing_service.AboAbschlussOffen):
        await _wechsel(uid, plan_b)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE abo_abschluesse SET angelegt_am = NOW() - interval '16 minutes' "
            "WHERE user_id=$1 AND status='reserviert'", uid,
        )
    neu = await _wechsel(uid, plan_b)
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM abo_abschluesse WHERE user_id=$1 AND status='verworfen'", uid) == 1
    await billing_service.handle_webhook(neu["paymentId"])
    assert await _aktive(db, uid) == [plan_b]


async def test_gleichzeitige_wechselanfragen_nur_eine(db):
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    erg = await asyncio.gather(
        _wechsel(uid, plan_b), _wechsel(uid, (plan_a[0], 3)), return_exceptions=True,
    )
    ok = [e for e in erg if isinstance(e, dict)]
    abgewiesen = [e for e in erg if isinstance(e, billing_service.AboAbschlussOffen)]
    assert len(ok) == 1 and len(abgewiesen) == 1, erg


async def test_wechsel_ersetzt_nur_abo_derselben_app(db):
    """Konto = Nutzer + App: ein juengeres Mollie-Abo einer ANDEREN App wird
    vom Wechsel nicht angefasst (bis 18.10. nahm change_subscription das
    juengste aktive Abo irgendeiner App)."""
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    async with db.acquire() as conn:
        fremd = await conn.fetchval(
            """INSERT INTO subscriptions (user_id, app_id, plan_id, status, mollie_customer_id,
                   mollie_subscription_id, mollie_first_payment_id, seats, started_at)
               VALUES ($1, 'engelmann', 'engelmann-custom', 'active', 'cst_f', $2, $2, 1,
                       NOW() + interval '1 hour')
               RETURNING id""",
            uid, "f" + uuid.uuid4().hex,
        )
    w = await _wechsel(uid, plan_b)
    assert w["replacesSubscriptionId"] == alt
    await billing_service.handle_webhook(w["paymentId"])
    assert await _status(db, str(fremd)) == "active"
    assert await _status(db, alt) == "cancelled"


async def test_erstkauf_neben_aktivem_abo_409(db):
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    with pytest.raises(billing_service.AboSchonAktiv):
        await billing_service.start_subscription_checkout(
            str(uid), plan_b[0], 2, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
        )


async def test_gleichzeitige_erstkaeufe_ergeben_ein_abo(db):
    """Zwei Erstkauf-Checkouts eines Kontos ohne Abo: nur einer entsteht."""
    from src.billing import billing_service
    from tests.billing.test_energy_credit_arten_echte_db import _bestandskunde_mit_altguthaben
    from tests.billing.test_planwechsel_echte_db import _monatsplan

    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        plan = await _monatsplan(conn)

    async def kauf(seats):
        return await billing_service.start_subscription_checkout(
            str(uid), plan, seats, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
        )

    erg = await asyncio.gather(kauf(1), kauf(2), return_exceptions=True)
    ok = [e for e in erg if isinstance(e, dict)]
    assert len(ok) == 1, erg
    await billing_service.handle_webhook(ok[0]["paymentId"])
    assert len(await _aktive(db, uid)) == 1


# ---------------------------------------------------------------------------
# Befund 2 — dauerhafter Kuendigungsfehler: sichtbar, Alarm, Nachholauftrag
# ---------------------------------------------------------------------------

async def test_dauerhafter_kuendigungsfehler_doppelt_aktiv_und_nachholen(db, caplog):
    from src.billing import billing_service
    from src.billing.routes import list_abo_abschluesse

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    alt_mollie = await _mollie_id(db, alt)
    w = await _wechsel(uid, plan_b)
    kaputt = AsyncMock(side_effect=RuntimeError("mollie 503"))
    with patch.object(_mollie(), "cancel_subscription", kaputt):
        for _ in range(3):
            with pytest.raises(billing_service.AboDoppeltAktiv):
                await billing_service.handle_webhook(w["paymentId"])
        bericht = await billing_service.wechsel_kuendigungen_nachholen()   # 4. Versuch
        assert w["paymentId"] in bericht["doppeltAktiv"]
    ab = await _abschluss(db, w["paymentId"])
    assert ab["status"] == "doppelt_aktiv"
    assert ab["kuendigung_versuche"] == 4
    assert ab["doppelt_aktiv_seit"] is not None
    assert "mollie 503" in ab["letzter_fehler"]
    # Nie still: Alarm im Log bei JEDEM Versuch, Ereignis genau einmal.
    assert sum("[ALARM][abo-wechsel]" in r.getMessage() and alt in r.getMessage()
               for r in caplog.records) == 4
    assert await _events(db, uid, "subscription.double_active") == 1
    # Sichtbar fuer den Operator.
    liste = (await list_abo_abschluesse(status="doppelt_aktiv", limit=1000))["abschluesse"]
    assert any(e["payment_id"] == w["paymentId"] for e in liste)
    # Kein Abo verloren: beide laufen.
    assert sorted(await _aktive(db, uid)) == sorted([plan_a, plan_b])
    assert _mollie_status(alt_mollie) == "active"
    # Weiterer Wechsel ist gesperrt, solange der Zustand offen ist.
    with pytest.raises(billing_service.AboAbschlussOffen):
        await _wechsel(uid, (plan_a[0], 3))

    # Mollie wieder erreichbar: Nachholauftrag schliesst ab.
    bericht = await billing_service.wechsel_kuendigungen_nachholen()
    assert bericht["abgeschlossen"] >= 1 and w["paymentId"] not in bericht["doppeltAktiv"]
    ab = await _abschluss(db, w["paymentId"])
    assert (ab["status"], ab["ergebnis"]) == ("abgeschlossen", "alt_gekuendigt")
    assert await _status(db, alt) == "cancelled"
    assert _mollie_status(alt_mollie) == "canceled"
    assert await _aktive(db, uid) == [plan_b]
    assert await _events(db, uid, "subscription.double_active_resolved") == 1
    # Der Ersatz hat seinen Vermerk verloren (Trigger) und belegt den Platz.
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT ueberlappt_abo_id FROM subscriptions WHERE id=$1", ab["neu_abo_id"]) is None


# ---------------------------------------------------------------------------
# Befund 3 — nie das letzte Abo kuendigen
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ersatz_weg", ["vom_kunden_gekuendigt", "gesperrt"])
async def test_retry_nach_ende_des_ersatzes_laesst_altes_abo(db, ersatz_weg):
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    alt_mollie = await _mollie_id(db, alt)
    w = await _wechsel(uid, plan_b)
    with patch.object(_mollie(), "cancel_subscription", AsyncMock(side_effect=RuntimeError("mollie 503"))):
        with pytest.raises(billing_service.AboDoppeltAktiv):
            await billing_service.handle_webhook(w["paymentId"])
    neu = (await _abschluss(db, w["paymentId"]))["neu_abo_id"]
    if ersatz_weg == "vom_kunden_gekuendigt":
        await billing_service.cancel_subscription(str(uid), str(neu))   # Route routes.py /cancel
    else:
        await billing_service._suspend_subscription_by_mollie_id(
            await _mollie_id(db, neu), "tr_folge", "failed")

    res = await billing_service.handle_webhook(w["paymentId"])          # spaeter Retry
    assert res["replacedSubscriptionId"] is None
    assert res["wechsel"] == {"status": "abgeschlossen", "ergebnis": "ersatz_nicht_aktiv"}
    assert await _status(db, alt) == "active"
    assert _mollie_status(alt_mollie) == "active"
    assert await _aktive(db, uid) == [plan_a]
    assert await _events(db, uid, "subscription.change_kept_old") == 1
    # Und der Nachholauftrag fasst den Fall nicht mehr an.
    await billing_service.wechsel_kuendigungen_nachholen()
    assert await _status(db, alt) == "active"


async def test_altes_abo_inzwischen_beendet_keine_kuendigung(db):
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    w = await _wechsel(uid, plan_b)
    await billing_service.cancel_subscription(str(uid), alt)            # Kunde kuendigt selbst
    kuendigen = AsyncMock()
    with patch.object(_mollie(), "cancel_subscription", kuendigen):
        res = await billing_service.handle_webhook(w["paymentId"])
    kuendigen.assert_not_awaited()
    assert res["wechsel"] == {"status": "abgeschlossen", "ergebnis": "alt_schon_beendet"}
    assert await _aktive(db, uid) == [plan_b]


async def test_kunde_kuendigt_altes_waehrend_doppelt_aktiv(db):
    """Waehrend 'doppelt_aktiv' kuendigt der Kunde das alte Abo selbst. Die
    Wiederholung kuendigt nicht ein zweites Mal und schliesst ab."""
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    w = await _wechsel(uid, plan_b)
    with patch.object(_mollie(), "cancel_subscription", AsyncMock(side_effect=RuntimeError("mollie 503"))):
        with pytest.raises(billing_service.AboDoppeltAktiv):
            await billing_service.handle_webhook(w["paymentId"])
    await billing_service.cancel_subscription(str(uid), alt)            # Kunde, Route /cancel
    kuendigen = AsyncMock()
    with patch.object(_mollie(), "cancel_subscription", kuendigen):
        res = await billing_service.handle_webhook(w["paymentId"])
    kuendigen.assert_not_awaited()
    assert res["wechsel"] == {"status": "abgeschlossen", "ergebnis": "alt_schon_beendet"}
    assert await _aktive(db, uid) == [plan_b]


# ---------------------------------------------------------------------------
# Befund 4 — konkurrierende identische Webhooks, kein Geisterabo
# ---------------------------------------------------------------------------

async def test_gleichzeitige_webhooks_ein_mollie_abo(db):
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    w = await _wechsel(uid, plan_b)
    echt = _mollie().create_subscription

    async def langsam(**kw):
        await asyncio.sleep(0.3)   # Fenster weit offen halten
        return await echt(**kw)

    with patch.object(_mollie(), "create_subscription", side_effect=langsam) as erzeugt:
        erg = await asyncio.gather(*(billing_service.handle_webhook(w["paymentId"]) for _ in range(4)))
    assert erzeugt.await_count == 1
    assert len({e["subscription"]["id"] for e in erg}) == 1
    assert len(_mollie_abos_zu(w["paymentId"])) == 1
    assert await _aktive(db, uid) == [plan_b]
    assert await _status(db, alt) == "cancelled"


async def test_zweite_verbindung_wartet_auf_die_abschluss_sperre(db):
    """Zwei echte Verbindungen: solange die erste den Abschluss sperrt, kommt die
    zweite nicht an ihm vorbei (lock_timeout statt Durchlauf)."""
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    w = await _wechsel(uid, plan_b)
    eins = await asyncpg.connect(PG_URL)
    zwei = await asyncpg.connect(PG_URL)
    try:
        tr = eins.transaction()
        await tr.start()
        await eins.fetchrow("SELECT id FROM abo_abschluesse WHERE payment_id=$1 FOR UPDATE", w["paymentId"])
        await zwei.execute("SET lock_timeout = '300ms'")
        with pytest.raises(asyncpg.LockNotAvailableError):
            async with zwei.transaction():
                await zwei.fetchrow(
                    "SELECT id FROM abo_abschluesse WHERE payment_id=$1 FOR UPDATE", w["paymentId"])
        await tr.rollback()
    finally:
        await eins.close()
        await zwei.close()


async def test_absturz_nach_mollie_anlage_kein_zweites_abo(db):
    """Mollie legte das Abo an, die Zeile wurde nie geschrieben (Rollback). Die
    Wiederholung findet es (firstPaymentId) statt ein zweites anzulegen."""
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    w = await _wechsel(uid, plan_b)
    with patch("src.billing.billing_service._ensure_monthly_budget_pot",
               AsyncMock(side_effect=ConnectionError("db weg"))):
        with pytest.raises(ConnectionError):
            await billing_service.handle_webhook(w["paymentId"])
    assert len(_mollie_abos_zu(w["paymentId"])) == 1
    assert await _aktive(db, uid) == [plan_a]           # nichts halb geschrieben
    assert (await _abschluss(db, w["paymentId"]))["status"] == "offen"

    with patch.object(_mollie(), "create_subscription", AsyncMock()) as erzeugt:
        await billing_service.handle_webhook(w["paymentId"])
    erzeugt.assert_not_awaited()
    assert len(_mollie_abos_zu(w["paymentId"])) == 1
    assert await _aktive(db, uid) == [plan_b]


async def test_idempotenzschluessel_wird_mitgegeben(db):
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    w = await _wechsel(uid, plan_b)
    echt = _mollie().create_subscription
    with patch.object(_mollie(), "create_subscription", side_effect=echt) as erzeugt:
        await billing_service.handle_webhook(w["paymentId"])
    kw = erzeugt.await_args.kwargs
    assert kw["idempotency_key"] == f"abo-erstzahlung-{w['paymentId']}"
    assert kw["metadata"]["firstPaymentId"] == w["paymentId"]


# ---------------------------------------------------------------------------
# Die Invariante in der Tabelle selbst
# ---------------------------------------------------------------------------

async def test_tabelle_verbietet_zweites_aktives_mollie_abo(db):
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.UniqueViolationError) as exc:
            await conn.execute(
                """INSERT INTO subscriptions (user_id, app_id, plan_id, status, mollie_customer_id,
                       mollie_subscription_id, mollie_first_payment_id, seats)
                   SELECT user_id, app_id, plan_id, 'active', mollie_customer_id, $2, $2, 1
                     FROM subscriptions WHERE id=$1""",
                uuid.UUID(alt), "x" + uuid.uuid4().hex,
            )
        assert exc.value.constraint_name == "subscriptions_ein_mollie_abo_je_konto"
        # Testphase/Vergabe ohne Mollie-Abo zaehlt nicht.
        await conn.execute(
            """INSERT INTO subscriptions (user_id, app_id, plan_id, status, mollie_customer_id, seats)
               SELECT user_id, app_id, plan_id, 'active', 'seed-x', 1 FROM subscriptions WHERE id=$1""",
            uuid.UUID(alt),
        )


async def test_tabelle_hoechstens_ein_ersatz_und_nur_mit_offenem_wechsel(db):
    uid, a, b, pid = await _paar_doppelt_aktiv(db)
    async with db.acquire() as conn:
        # Zweiter Ersatz neben dem echten: abgewiesen.
        with pytest.raises((asyncpg.UniqueViolationError, asyncpg.CheckViolationError)):
            await conn.execute(_ERSATZ_SQL, a, "e2" + uuid.uuid4().hex, a)
        # Altes endet -> Vermerk faellt (Trigger), Ersatz ist das eine Abo.
        await conn.execute("UPDATE subscriptions SET status='cancelled' WHERE id=$1", a)
        assert await conn.fetchval("SELECT ueberlappt_abo_id FROM subscriptions WHERE id=$1", b) is None
        # Vermerk auf ein nicht aktives Abo: abgewiesen.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(_ERSATZ_SQL, a, "e3" + uuid.uuid4().hex, a)


async def test_tabelle_ein_offener_abschluss_je_konto(db):
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    async with db.acquire() as conn:
        app = await conn.fetchval("SELECT app_id FROM subscriptions WHERE id=$1", uuid.UUID(alt))
        sql = """INSERT INTO abo_abschluesse (user_id, app_id, plan_id, seats, status)
                 VALUES ($1, $2::app_id, $3::plan_id, 1, 'reserviert')"""
        await conn.execute(sql, uid, app, plan_a[0])
        with pytest.raises(asyncpg.UniqueViolationError) as exc:
            await conn.execute(sql, uid, app, plan_a[0])
        assert exc.value.constraint_name == "abo_abschluesse_ein_offener_je_konto"


async def test_migration_070_bricht_bei_bestandsdubletten_ab(db):
    """Bestand von vor 070 nachgestellt (Index und Trigger in der Transaktion
    entfernt), dann 070: laut abgebrochen, nichts bereinigt."""
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    sql = MIGRATION_070.read_text()
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.RaiseError, match=r"070: \d+ Konto/Konten mit mehr als einem"):
            async with conn.transaction():
                await conn.execute("DROP INDEX subscriptions_ein_mollie_abo_je_konto")
                await conn.execute("DROP TRIGGER abo_zz_konto_invariante ON subscriptions")
                await conn.execute("DROP TRIGGER abo_00_subscriptions_sperre ON subscriptions")
                await conn.execute(
                    """INSERT INTO subscriptions (user_id, app_id, plan_id, status, mollie_customer_id,
                           mollie_subscription_id, mollie_first_payment_id, seats)
                       SELECT user_id, app_id, plan_id, 'active', mollie_customer_id, $2, $2, 1
                         FROM subscriptions WHERE id=$1""",
                    uuid.UUID(alt), "d" + uuid.uuid4().hex,
                )
                await conn.execute(sql)
        # Rollback: Index und Trigger wieder da.
        assert await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE indexname='subscriptions_ein_mollie_abo_je_konto'") == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname IN ('abo_zz_konto_invariante','abo_00_subscriptions_sperre')") == 3


# ---------------------------------------------------------------------------
# R3 — Invariante unabhaengig vom Codepfad (Codex-Pruefung R2, Befunde A und B)
# ---------------------------------------------------------------------------

_ERSATZ_SQL = """INSERT INTO subscriptions (user_id, app_id, plan_id, status, mollie_customer_id,
                     mollie_subscription_id, mollie_first_payment_id, seats, ueberlappt_abo_id)
                 SELECT user_id, app_id, plan_id, 'active', mollie_customer_id, $2, $2, 1, $3
                   FROM subscriptions WHERE id=$1"""
_REGULAER_SQL = """INSERT INTO subscriptions (user_id, app_id, plan_id, status, mollie_customer_id,
                       mollie_subscription_id, mollie_first_payment_id, seats)
                   SELECT user_id, app_id, plan_id, 'active', mollie_customer_id, $2, $2, 1
                     FROM subscriptions WHERE id=$1"""


async def _paar_doppelt_aktiv(db):
    """Gueltige Ersatzbeziehung ueber den echten Weg: Wechsel bezahlt, Kuendigung
    des alten Abos gescheitert -> A und B aktiv, B.ueberlappt_abo_id = A,
    Abschluss doppelt_aktiv."""
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    w = await _wechsel(uid, plan_b)
    with patch.object(_mollie(), "cancel_subscription", AsyncMock(side_effect=RuntimeError("mollie 503"))):
        with pytest.raises(billing_service.AboDoppeltAktiv):
            await billing_service.handle_webhook(w["paymentId"])
    ab = await _abschluss(db, w["paymentId"])
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT ueberlappt_abo_id FROM subscriptions WHERE id=$1", ab["neu_abo_id"]) == uuid.UUID(alt)
    return uid, uuid.UUID(alt), ab["neu_abo_id"], w["paymentId"]


async def _aktive_mollie(conn, uid):
    return await conn.fetchval(
        """SELECT count(*) FROM subscriptions WHERE user_id=$1 AND status='active'
              AND mollie_subscription_id IS NOT NULL""", uid)


@pytest.mark.parametrize("ziel,sql", [
    ("alt", "UPDATE subscriptions SET app_id='engelmann' WHERE id=$1"),
    ("neu", "UPDATE subscriptions SET app_id='engelmann' WHERE id=$1"),
    ("alt", "UPDATE subscriptions SET mollie_subscription_id=NULL WHERE id=$1"),
    ("neu", "UPDATE subscriptions SET mollie_subscription_id='anders' WHERE id=$1"),
    ("alt", "UPDATE subscriptions SET user_id=gen_random_uuid() WHERE id=$1"),
])
async def test_codex_direkte_updates_an_kontofeldern_abgewiesen(db, ziel, sql):
    """Codex R2 Befund B: direkte UPDATEs an beiden Seiten der Beziehung."""
    uid, a, b, pid = await _paar_doppelt_aktiv(db)
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(sql, a if ziel == "alt" else b)
        zeilen = await conn.fetch(
            "SELECT id, app_id, mollie_subscription_id, ueberlappt_abo_id FROM subscriptions WHERE id = ANY($1)",
            [a, b])
    assert {r["app_id"] for r in zeilen} == {"werking-report"}
    assert all(r["mollie_subscription_id"] for r in zeilen)


async def test_codex_ersatz_ohne_offenen_wechsel_abgewiesen(db):
    """Ein Ersatzvermerk ohne offenen Wechsel (gewoehnliches INSERT) scheitert."""
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError, match="ohne offenen Wechsel"):
            await conn.execute(_ERSATZ_SQL, uuid.UUID(alt), "x" + uuid.uuid4().hex, uuid.UUID(alt))
        assert await _aktive_mollie(conn, uid) == 1


async def test_codex_rennen_ersatz_insert_gegen_kuendigung_mit_lock_timeout(db):
    """Codex R2 Befund A, wortgleich: Verbindung 1 fuegt den Ersatz ein und haelt
    den Commit, Verbindung 2 kuendigt den Vorgaenger. Verbindung 2 kommt nicht an
    der Kontosperre vorbei; Verbindung 1 scheitert zum Commit (kein offener Wechsel)."""
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    a = uuid.UUID(alt)
    c1 = await asyncpg.connect(PG_URL)
    c2 = await asyncpg.connect(PG_URL)
    try:
        tr = c1.transaction()
        await tr.start()
        await c1.execute(_ERSATZ_SQL, a, "race" + uuid.uuid4().hex, a)
        await c2.execute("SET lock_timeout='500ms'")
        with pytest.raises(asyncpg.LockNotAvailableError):
            await c2.execute("UPDATE subscriptions SET status='cancelled' WHERE id=$1", a)
        with pytest.raises(asyncpg.CheckViolationError, match="ohne offenen Wechsel"):
            await tr.commit()
        # Ein weiteres regulaeres Abo neben A: abgewiesen.
        with pytest.raises((asyncpg.UniqueViolationError, asyncpg.CheckViolationError)):
            await c2.execute(_REGULAER_SQL, a, "extra" + uuid.uuid4().hex)
        assert await _aktive_mollie(c2, uid) == 1
        assert await _status(db, alt) == "active"
    finally:
        await c1.close()
        await c2.close()


async def test_codex_rennen_ohne_timeout_kuendigung_wartet_und_loest_auf(db):
    """Dasselbe Rennen mit gueltigem Wechsel und ohne lock_timeout: Verbindung 2
    wartet, bis Verbindung 1 committet, und loest DANN den Vermerk auf. Danach
    genau ein aktives Mollie-Abo, und kein zweites laesst sich einfuegen."""
    from src.billing import billing_service

    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    a = uuid.UUID(alt)
    w = await _wechsel(uid, plan_b)
    ab_id = (await _abschluss(db, w["paymentId"]))["id"]
    c1 = await asyncpg.connect(PG_URL)
    c2 = await asyncpg.connect(PG_URL)
    try:
        tr = c1.transaction()
        await tr.start()
        b = await c1.fetchval(_ERSATZ_SQL + " RETURNING id", a, "neu" + uuid.uuid4().hex, a)
        await c1.execute(
            "UPDATE abo_abschluesse SET neu_abo_id=$2, status='neu_aktiv' WHERE id=$1", ab_id, b)
        kuendigung = asyncio.create_task(
            c2.execute("UPDATE subscriptions SET status='cancelled' WHERE id=$1", a))
        await asyncio.sleep(0.4)
        assert not kuendigung.done(), "Kuendigung lief an der Kontosperre vorbei"
        await tr.commit()
        await asyncio.wait_for(kuendigung, 10)
        assert await c2.fetchval("SELECT ueberlappt_abo_id FROM subscriptions WHERE id=$1", b) is None
        assert await _aktive_mollie(c2, uid) == 1
        with pytest.raises((asyncpg.UniqueViolationError, asyncpg.CheckViolationError)):
            await c2.execute(_REGULAER_SQL, a, "extra" + uuid.uuid4().hex)
    finally:
        await c1.close()
        await c2.close()
    res = await billing_service._wechsel_abschliessen(w["paymentId"])
    assert res["ergebnis"] == "alt_schon_beendet"


async def test_abschluss_schliessen_loest_ersatz_in_derselben_transaktion(db):
    uid, a, b, pid = await _paar_doppelt_aktiv(db)
    async with db.acquire() as conn:
        # Schliessen, waehrend beide laufen: abgewiesen (zwei regulaere Abos).
        with pytest.raises((asyncpg.UniqueViolationError, asyncpg.CheckViolationError)):
            await conn.execute(
                "UPDATE abo_abschluesse SET status='abgeschlossen', ergebnis='x' WHERE payment_id=$1", pid)
        # Loeschen des offenen Wechsels: ebenso.
        with pytest.raises((asyncpg.UniqueViolationError, asyncpg.CheckViolationError)):
            await conn.execute("DELETE FROM abo_abschluesse WHERE payment_id=$1", pid)
        # Altes kuendigen UND schliessen in einer Transaktion: erlaubt, Vermerk weg.
        async with conn.transaction():
            await conn.execute("UPDATE subscriptions SET status='cancelled' WHERE id=$1", a)
            await conn.execute(
                "UPDATE abo_abschluesse SET status='abgeschlossen', ergebnis='alt_gekuendigt' WHERE payment_id=$1", pid)
        assert await conn.fetchval("SELECT ueberlappt_abo_id FROM subscriptions WHERE id=$1", b) is None
        assert await _aktive_mollie(conn, uid) == 1


async def test_ersatz_bleibt_nach_schliessen_nicht_stehen(db):
    """Ersatz gesperrt, Wechsel endet mit ersatz_nicht_aktiv: Vermerk weg, und ein
    spaeteres Wiederaufleben des Ersatzes neben dem alten scheitert."""
    from src.billing import billing_service

    uid, a, b, pid = await _paar_doppelt_aktiv(db)
    await billing_service._suspend_subscription_by_mollie_id(await _mollie_id(db, b), "tr_f", "failed")
    res = await billing_service.handle_webhook(pid)
    assert res["wechsel"]["ergebnis"] == "ersatz_nicht_aktiv"
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT ueberlappt_abo_id FROM subscriptions WHERE id=$1", b) is None
        with pytest.raises((asyncpg.UniqueViolationError, asyncpg.CheckViolationError)):
            await conn.execute("UPDATE subscriptions SET status='active' WHERE id=$1", b)


async def test_repeatable_read_wird_abgewiesen(db):
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError, match="READ COMMITTED"):
            async with conn.transaction(isolation="repeatable_read"):
                await conn.execute("UPDATE subscriptions SET status='suspended' WHERE id=$1", uuid.UUID(alt))
        assert await _status(db, alt) == "active"


async def test_deferred_pruefung_greift_auch_ohne_indizes(db):
    """Die Commit-Pruefung allein (Indizes in der Transaktion entfernt) haelt
    die Invariante: zweites regulaeres Mollie-Abo wird abgewiesen."""
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError, match="Abo-Invariante verletzt"):
            async with conn.transaction():
                await conn.execute("DROP INDEX subscriptions_ein_mollie_abo_je_konto")
                await conn.execute("DROP INDEX subscriptions_hoechstens_ein_ersatz_je_konto")
                await conn.execute(_REGULAER_SQL, uuid.UUID(alt), "y" + uuid.uuid4().hex)
        assert await _aktive_mollie(conn, uid) == 1
