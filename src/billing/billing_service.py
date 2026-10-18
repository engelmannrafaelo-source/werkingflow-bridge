"""
BillingService — orchestriert Subscriptions + Top-Ups + Mollie + DB.

Webhook-Handler: Mollie ruft /v1/billing/mollie-webhook auf, wir branchen nach pending_payments.type.
- subscription_first -> Mollie-Subscription erstellen, in subscriptions persistieren
- topup -> credit_purchases persistieren + datiertes Lot in user_topup_lots (FIFO, 12-Mon-Verfall)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from src.config import config
from src.db.client import get_pool
from src.billing.mollie_adapter import get_mollie_adapter
from src.billing.invoice_numbering import (
    FALLBACK_PREFIX,
    TOPUP_PREFIX,
    next_invoice_number,
    prefix_for_app,
)
# Als Modul importiert, nicht als Einzelfunktion: der Name `vat_id` ist in
# diesem Modul mehrfach als lokale Variable belegt (Rechnungsadresse), ein
# `from ... import has_confirmed_validation` waere dort leicht zu verwechseln.
from src.billing import vat_id as vat_id_module

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tax-rate determination — based on recipient billing address
# ---------------------------------------------------------------------------

# EU member states, ISO 3166-1 alpha-2.  AT is excluded: it uses the domestic
# rate and needs no Reverse Charge note.
_EU_COUNTRIES_NON_AT: frozenset[str] = frozenset({
    "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT",
    "NL", "PL", "PT", "RO", "SE", "SI", "SK",
})


def _determine_tax_rate(
    billing_address: Optional[Dict[str, Any]],
    *,
    vat_id_confirmed: bool,
) -> Tuple[float, Optional[str]]:
    """Return (tax_rate_percent, reverse_charge_note_or_None).

    `vat_id_confirmed` ist ein PFLICHT-Keyword ohne Default: ob eine UID gegen
    VIES bestaetigt wurde, ist eine Tatsache aus der Datenbank
    (vat_id.has_confirmed_validation), keine Annahme. Ein Default waere die
    Stelle, an der ein neuer Aufrufer versehentlich wieder 0 % auf eine
    ungeprueste Nummer gibt — genau der Fehler, den Migration 055 behebt.

    Rules:
    - No address or no country → 20.0 AT domestic (safe default, explicit)
    - AT → 20.0 (domestic USt)
    - EU (non-AT) + VIES-BESTAETIGTE vatId → 0.0, Reverse Charge (B2B)
    - EU (non-AT) + vatId vorhanden, aber nicht bestaetigt → 20.0 (safe default).
      Bis 2026-08-05 genuegte hier ein beliebiger nicht-leerer Text. § 19 Abs 1
      UStG verlagert die Steuerschuld aber nur bei einer GUELTIGEN UID; war sie
      es nicht, schuldet der Aussteller die Steuer — rueckwirkend und erst bei
      der Pruefung sichtbar.
    - EU (non-AT) without vatId → 20.0 AT (safe default)
      OPEN DECISION: EU B2C falls under the OSS-Verfahren; correct rate per
      destination country is a legal question not answered here.  We use 20%
      AT as the conservative safe default and mark the invoice metadata with
      {"taxNote": "EU_B2C_OSS_OPEN"} so operators can identify these cases.
      This is intentional and must be resolved with a tax advisor before
      serving significant EU B2C volume.
    - Non-EU → 0.0 (export, no VAT)

    Die VIES-Abfrage selbst laeuft NICHT hier, sondern beim Speichern der
    Rechnungsadresse (src/billing/vat_id.py). Diese Funktion schlaegt nur das
    Ergebnis nach. Grund: VIES ist ein fremder Dienst mit Ausfaellen — haengt
    die Rechnungserstellung daran, steht im Ausfall der Verkauf, oder jemand
    baut einen Fallback ein, und genau der wird zur stillen Nullsteuer-Quelle.
    """
    if not billing_address:
        return 20.0, None

    country = (billing_address.get("country") or "").upper().strip()
    vat_id = (billing_address.get("vatId") or "").strip()

    if not country:
        return 20.0, None

    if country == "AT":
        return 20.0, None

    if country in _EU_COUNTRIES_NON_AT:
        if vat_id and vat_id_confirmed:
            return 0.0, (
                "Steuerschuldnerschaft des Leistungsempfängers "
                "gem. § 19 Abs. 1 UStG (Reverse Charge)"
            )
        if vat_id:
            # UID erfasst, aber nicht (mehr) bestaetigt: sicherer Default statt
            # Nullsteuer. Die Adress-Route prueft beim Speichern; findet sich
            # hier nichts, war die Pruefung negativ, zu alt oder VIES war aus.
            return 20.0, None
        # EU B2C — OSS open decision, conservative AT default
        return 20.0, None

    # Non-EU export
    return 0.0, None


# ---------------------------------------------------------------------------
# Rechnungsbetraege — EINE Quelle fuer Rechnung UND Mollie-Betrag
# ---------------------------------------------------------------------------
#
# Rafael-Entscheid 17.10.2026 (e-energy-mollie-brutto-20261017 = a): Mollie
# zieht brutto ein (Nettopreis + USt.), damit abgebuchter Betrag und Rechnung
# uebereinstimmen. Katalogpreise sind netto (plans.price_basis, Migration 066).
# Steuersatz: _determine_tax_rate aus der Rechnungsadresse (resolve_invoice_tax).
# Rundung: genau eine Stelle — die Steuer wird kaufmaennisch (ROUND_HALF_UP) auf
# Cent gerundet; Netto ist bereits ein Centbetrag, Brutto = Netto + Steuer ohne
# weitere Rundung. Bis 17.10.2026 rundete quantize() mit dem Kontext-Default
# ROUND_HALF_EVEN (Bankrundung) — bei glatten Katalogpreisen ohne Unterschied.

from decimal import Decimal, ROUND_HALF_UP  # noqa: E402

_CENT = Decimal("0.01")


def invoice_amounts(net_eur: Any, tax_rate: float) -> Tuple[Decimal, Decimal, Decimal]:
    """(subtotal, tax, total) einer Rechnung aus dem Nettobetrag.

    Wirft ValueError, wenn der Nettobetrag kein Centbetrag ist oder negativ —
    eine zweite, stille Rundung am Netto waere die zweite Rundungsstelle.
    """
    subtotal = Decimal(str(net_eur))
    if subtotal < 0 or subtotal != subtotal.quantize(_CENT):
        raise ValueError(f"net amount must be a non-negative cent amount, got {net_eur!r}")
    tax = (subtotal * Decimal(str(tax_rate)) / Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)
    return subtotal, tax, subtotal + tax


async def resolve_invoice_tax(user_id: str, *, log_prefix: str = "[invoice]") -> Dict[str, Any]:
    """Rechnungsadresse und Steuersatz des Kaeufers — dieselbe Aufloesung fuer
    auto_create_invoice, die Bestell-Rechnung (pending_orders_service) und die
    Bruttoanzeige vor dem Kauf (price_quote).

    Returns dict: tenantId, billingAddress (oder None), taxRate,
    reverseChargeNote (oder None), euB2c (bool).
    """
    pool = get_pool()
    tenant_id: str | None = None
    billing_address: Dict[str, Any] | None = None
    # Failure is non-fatal: invoice is created with NULL billing_address and
    # marked incomplete (Bestandsverhalten).
    try:
        async with pool.acquire() as conn:
            trow = await conn.fetchrow(
                """
                SELECT u.tenant_id,
                       t.billing_name, t.billing_street, t.billing_city,
                       t.billing_postcode, t.billing_country, t.billing_vat_id
                FROM users u
                LEFT JOIN tenants t ON t.id = u.tenant_id
                WHERE u.id = $1
                """,
                uuid.UUID(user_id),
            )
        if trow and trow["tenant_id"]:
            tenant_id = trow["tenant_id"]
            if trow["billing_name"]:
                billing_address = {
                    "name": trow["billing_name"],
                    "street": trow["billing_street"],
                    "city": trow["billing_city"],
                    "postcode": trow["billing_postcode"],
                    "country": (trow["billing_country"] or "").upper().strip() or None,
                    "vatId": trow["billing_vat_id"],
                }
    except Exception as e:  # noqa: BLE001 — silent-ok: Bestandsverhalten, Rechnung wird als incomplete markiert
        logger.warning("%s billing address lookup failed for user=%s: %s", log_prefix, user_id, e)

    # Wurde die erfasste UID gegen VIES bestaetigt? Nur dann darf Reverse
    # Charge greifen. Ein Fehler beim Nachschlagen bedeutet "nicht bestaetigt",
    # nie "schon in Ordnung" — die teure Richtung ist die Nullsteuer.
    vat_id_confirmed = False
    if tenant_id and billing_address and (billing_address.get("vatId") or "").strip():
        try:
            async with pool.acquire() as conn:
                vat_id_confirmed = await vat_id_module.has_confirmed_validation(
                    conn, tenant_id=str(tenant_id), vat_id=billing_address["vatId"],
                )
        except Exception as e:
            logger.warning(
                "%s VIES confirmation lookup failed for tenant=%s: %s — "
                "invoicing WITH VAT (safe default)", log_prefix, tenant_id, e,
            )

    tax_rate, reverse_charge_note = _determine_tax_rate(
        billing_address, vat_id_confirmed=vat_id_confirmed,
    )

    # Detect EU B2C (no vatId in EU non-AT country) — mark for operator review.
    eu_b2c = False
    if billing_address:
        country = (billing_address.get("country") or "").upper()
        vat_id = (billing_address.get("vatId") or "").strip()
        eu_b2c = country in _EU_COUNTRIES_NON_AT and not vat_id

    return {
        "tenantId": tenant_id,
        "billingAddress": billing_address,
        "taxRate": tax_rate,
        "reverseChargeNote": reverse_charge_note,
        "euB2c": eu_b2c,
    }


async def price_quote(user_id: str, plan_id: str, quantity: int) -> Dict[str, Any]:
    """Bruttobetrag eines Kaufs VOR dem Checkout — fuer die Kauf-/Kassenanzeige.

    Gleiche Rechnung wie die Bestell-Rechnung (invoice_amounts +
    resolve_invoice_tax). Massgeblich bleibt der Betrag der Rechnung, die der
    Checkout anlegt; aendert sich die Rechnungsadresse dazwischen, zieht Mollie
    den Betrag dieser Rechnung ein, nicht den des Angebots.
    """
    from src.budget.plans import require_sellable
    plan = require_sellable(plan_id)
    if quantity < 1:
        raise ValueError("quantity must be >= 1")
    tax = await resolve_invoice_tax(user_id, log_prefix="[price-quote]")
    net = Decimal(str(plan.price)) * quantity
    subtotal, tax_eur, total = invoice_amounts(net, tax["taxRate"])
    return {
        "planId": plan_id,
        "quantity": quantity,
        "netEur": float(subtotal),
        "taxRate": tax["taxRate"],
        "taxEur": float(tax_eur),
        "grossEur": float(total),
        "reverseChargeNote": tax["reverseChargeNote"],
        "billingAddressComplete": tax["billingAddress"] is not None,
    }


async def _order_invoice_gross(order: Dict[str, Any], *, expected_net: float) -> float:
    """Mollie-Betrag einer Self-Service-Bestellung = total_eur ihrer Rechnung.

    Die Rechnung (Entwurf) legt create_pending_order VOR der Zahlung an; ihr
    Brutto ist der einzige Betrag, den Mollie einziehen darf. Stimmt ihr Netto
    nicht mit dem Katalogpreis ueberein, ist etwas zwischen Bestellung und
    Rechnung auseinandergelaufen → laut scheitern, keine Zahlung.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT subtotal_eur, total_eur FROM invoices WHERE id = $1",
            uuid.UUID(str(order["invoiceId"])),
        )
    if not row:
        raise RuntimeError(f"order {order['id']}: invoice {order['invoiceId']} not found — no payment started")
    if Decimal(str(row["subtotal_eur"])) != Decimal(str(expected_net)).quantize(_CENT):
        raise RuntimeError(
            f"order {order['id']}: invoice subtotal {row['subtotal_eur']} != catalog net "
            f"{expected_net} — no payment started"
        )
    return float(Decimal(str(row["total_eur"])))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------

def _serialize_customer(row: Any) -> Dict[str, Any]:
    return {
        "userId": str(row["user_id"]),
        "mollieCustomerId": row["mollie_customer_id"],
        "email": row["email"],
        "name": row["name"],
        "createdAt": row["created_at"].isoformat(),
    }


async def get_or_create_customer(user_id: str, email: str, name: str) -> Dict[str, Any]:
    """
    Idempotent customer provisioning.

    The lookup→Mollie-create→INSERT used to span three DB acquisitions with a
    network call in between (TOCTOU race C4). Two concurrent callers for the
    same user could each pass the SELECT, each call Mollie (creating two
    Mollie customers), then race the INSERT — one would orphan its Mollie
    customer. We now hold a single connection and use INSERT … ON CONFLICT
    DO NOTHING RETURNING; if the conflict path fires (another writer won),
    we re-SELECT inside the same connection.
    """
    pool = get_pool()
    user_uuid = uuid.UUID(user_id)

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT user_id, mollie_customer_id, email, name, created_at FROM mollie_customers WHERE user_id = $1",
            user_uuid,
        )
        if existing:
            return _serialize_customer(existing)

        # No customer yet. Call Mollie OUTSIDE the implicit transaction so
        # we don't hold a row lock during the network call. The INSERT
        # below uses ON CONFLICT to handle the race where a concurrent
        # caller created the row while we were waiting on Mollie.
        mollie = get_mollie_adapter()
        mollie_id = await mollie.create_customer(email=email, name=name)

        row = await conn.fetchrow(
            """
            INSERT INTO mollie_customers (user_id, mollie_customer_id, email, name, created_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (user_id) DO NOTHING
            RETURNING user_id, mollie_customer_id, email, name, created_at
            """,
            user_uuid, mollie_id, email, name,
        )
        if row:
            await log_billing_event(
                "customer.created",
                user_id=user_id,
                source="system",
                payload={"mollieCustomerId": str(row["mollie_customer_id"]), "email": email, "name": name},
            )
            return _serialize_customer(row)

        # Conflict: another writer raced us. Use the row they wrote.
        # The orphaned Mollie customer we just created stays in Mollie's
        # data — we don't attempt cleanup because that opens new race
        # surfaces; Mollie customers are cheap and idempotent on email.
        winner = await conn.fetchrow(
            "SELECT user_id, mollie_customer_id, email, name, created_at FROM mollie_customers WHERE user_id = $1",
            user_uuid,
        )
        if not winner:
            # Should never happen: ON CONFLICT fired but no row exists.
            raise RuntimeError(
                f"get_or_create_customer: race resolution failed for user {user_id}"
            )
        return _serialize_customer(winner)


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

def _plan_price(plan_id: str) -> float:
    """Source-of-truth: src/budget/plans.py. No local duplicate."""
    from src.budget.plans import get_plan
    return float(get_plan(plan_id).price)


from src.billing import purchase_consent


# ---------------------------------------------------------------------------
# Abo-Invariante je Abrechnungskonto (Migration 070)
# ---------------------------------------------------------------------------
#
# Konto = (user_id, app_id). Die Datenbank erzwingt: hoechstens EIN aktives
# Mollie-Abo je Konto, waehrend eines laufenden Wechsels zusaetzlich genau ein
# als Ersatz markiertes (subscriptions.ueberlappt_abo_id), und hoechstens EIN
# offener Abschluss (abo_abschluesse). Der Code bindet sich daran:
#   - jeder Abo-Checkout reserviert zuerst den einen Platz (zweiter -> 409),
#   - die Aktivierung laeuft unter der Zeilensperre des Abschlusses; das
#     Mollie-Abo entsteht hoechstens einmal (Sperre + Idempotenzschluessel +
#     Suche nach metadata.firstPaymentId),
#   - das alte Abo wird nur gekuendigt, wenn das neue aktiv ist und das alte
#     das im Wechsel gemerkte ist; scheitert die Kuendigung, steht der Wechsel
#     sichtbar auf 'doppelt_aktiv' (Alarm), der Nachholauftrag wiederholt.

ABSCHLUSS_OFFEN_SATZ = (
    "Für dieses Konto läuft bereits ein Abo-Abschluss oder Planwechsel. "
    "Ein weiterer ist erst möglich, wenn dieser bezahlt, abgebrochen oder verfallen ist."
)
ABO_SCHON_AKTIV_SATZ = (
    "Für dieses Konto ist bereits ein Abo aktiv. Bitte den Planwechsel verwenden."
)
# Eine Reservierung ohne Mollie-Checkout gilt danach als liegengeblieben
# (Absturz zwischen Reservierung und Checkout). Ohne Checkout-URL kann der
# Kunde nichts bezahlen; die Freigabe verliert also kein Geld.
RESERVIERUNG_VERFALL = timedelta(minutes=15)
_OFFENE_ABSCHLUESSE = ["reserviert", "offen", "neu_aktiv", "doppelt_aktiv"]
_ZAHLUNG_GESCHEITERT = ("failed", "expired", "canceled")


class AboAbschlussOffen(Exception):
    """Es laeuft schon ein Abschluss/Wechsel fuer dieses Konto (HTTP 409)."""

    def __init__(self) -> None:
        super().__init__(ABSCHLUSS_OFFEN_SATZ)


class AboSchonAktiv(Exception):
    """Erstkauf, obwohl schon ein Mollie-Abo aktiv ist (HTTP 409)."""

    def __init__(self) -> None:
        super().__init__(ABO_SCHON_AKTIV_SATZ)


class AboAbschlussKonflikt(RuntimeError):
    """Bezahlte Erstzahlung, die die Invariante verletzen wuerde. ALARM —
    der Kunde hat gezahlt, ein Mensch muss entscheiden (Webhook antwortet 500)."""


class AboDoppeltAktiv(RuntimeError):
    """Kuendigung des ersetzten Abos gescheitert: altes und neues laufen.
    Zustand steht in abo_abschluesse ('doppelt_aktiv'), Alarm ist ausgeloest;
    Webhook antwortet 500, damit auch Mollie wiederholt."""


def _konto_sperrschluessel(user_uuid: uuid.UUID, app_id: str) -> str:
    return f"abo-konto:{user_uuid}:{app_id}"


async def _abschluss_reservieren(
    user_uuid: uuid.UUID, app_id: str, plan_id: str, seats: int,
    alt_abo_id: Optional[uuid.UUID],
) -> uuid.UUID:
    """Belegt den einen Abschluss-Platz des Kontos, BEVOR Mollie etwas anlegt.

    Ein offener Platz wird nur freigegeben, wenn er nachweislich tot ist:
    Reservierung ohne Checkout aelter als RESERVIERUNG_VERFALL, oder Erstzahlung
    laut Mollie gescheitert. Sonst AboAbschlussOffen (409, fester Satz).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                _konto_sperrschluessel(user_uuid, app_id),
            )
            offen = await conn.fetchrow(
                """SELECT id, status, payment_id, angelegt_am FROM abo_abschluesse
                    WHERE user_id = $1 AND app_id = $2::app_id AND status = ANY($3::text[])
                    FOR UPDATE""",
                user_uuid, app_id, _OFFENE_ABSCHLUESSE,
            )
            if offen:
                neuer_status = None
                if (offen["status"] == "reserviert"
                        and offen["angelegt_am"] < datetime.now(timezone.utc) - RESERVIERUNG_VERFALL):
                    neuer_status = "verworfen"
                elif offen["status"] == "offen":
                    zahlung = await get_mollie_adapter().get_payment(offen["payment_id"])
                    if zahlung.get("status") in _ZAHLUNG_GESCHEITERT:
                        neuer_status = "abgebrochen"
                if neuer_status is None:
                    raise AboAbschlussOffen()
                await conn.execute(
                    """UPDATE abo_abschluesse SET status = $2, geaendert_am = NOW(),
                              letzter_fehler = 'beim naechsten Abschluss als tot erkannt'
                        WHERE id = $1""",
                    offen["id"], neuer_status,
                )
                logger.warning(
                    "[abo-abschluss] %s (%s) freigegeben als %s fuer neuen Abschluss",
                    offen["id"], offen["status"], neuer_status,
                )

            aktive = {r["id"] for r in await conn.fetch(
                """SELECT id FROM subscriptions
                    WHERE user_id = $1 AND app_id = $2::app_id AND status = 'active'
                      AND mollie_subscription_id IS NOT NULL""",
                user_uuid, app_id,
            )}
            if alt_abo_id is None:
                if aktive:
                    raise AboSchonAktiv()
            else:
                alt = await conn.fetchrow(
                    "SELECT user_id, app_id, status FROM subscriptions WHERE id = $1",
                    alt_abo_id,
                )
                if (not alt or alt["user_id"] != user_uuid or alt["app_id"] != app_id
                        or alt["status"] != "active"):
                    raise LookupError(f"Abo {alt_abo_id} ist kein aktives Abo dieses Kontos")
                if aktive - {alt_abo_id}:
                    # Kann bei geltender Invariante nicht vorkommen (ein Ersatz
                    # existiert nur bei offenem Abschluss, der oben ausgeschlossen ist).
                    raise AboAbschlussKonflikt(
                        f"Konto {user_uuid}/{app_id}: weiteres aktives Mollie-Abo neben {alt_abo_id}"
                    )
            return await conn.fetchval(
                """INSERT INTO abo_abschluesse (user_id, app_id, plan_id, seats, alt_abo_id, status)
                   VALUES ($1, $2::app_id, $3::plan_id, $4, $5, 'reserviert') RETURNING id""",
                user_uuid, app_id, plan_id, seats, alt_abo_id,
            )


async def _abschluss_checkout_eintragen(
    abschluss_id: uuid.UUID, payment_id: str, user_uuid: uuid.UUID, plan_id: str,
    amount: float, alt_uuid: Optional[uuid.UUID],
) -> None:
    """Erstzahlung und Abschluss in EINER Transaktion festschreiben. Ist die
    Reservierung inzwischen verfallen, wird die Checkout-URL nicht ausgegeben."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO pending_payments
                  (payment_id, user_id, type, plan_id, amount_eur, ersetzt_abo_id, created_at)
                VALUES ($1, $2, 'subscription_first', $3, $4, $5, NOW())
                ON CONFLICT (payment_id) DO NOTHING
                """,
                payment_id, user_uuid, plan_id, amount, alt_uuid,
            )
            done = await conn.execute(
                """UPDATE abo_abschluesse SET payment_id = $2, status = 'offen',
                          geaendert_am = NOW()
                    WHERE id = $1 AND status = 'reserviert'""",
                abschluss_id, payment_id,
            )
            if done != "UPDATE 1":
                raise RuntimeError(
                    f"Abschluss {abschluss_id} war nicht mehr reserviert ({done}) — "
                    "Checkout wird nicht ausgegeben"
                )


async def _abschluss_abbrechen(abschluss_id: uuid.UUID, grund: str) -> None:
    """Checkout nicht zustande gekommen: Platz freigeben. Scheitert schon das,
    bleibt die Reservierung liegen und verfaellt nach RESERVIERUNG_VERFALL."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE abo_abschluesse SET status = 'abgebrochen', letzter_fehler = $2,
                          geaendert_am = NOW()
                    WHERE id = $1 AND status = 'reserviert'""",
                abschluss_id, grund[:2000],
            )
    except Exception as exc:  # noqa: BLE001 — Ursprungsfehler wird vom Aufrufer weitergeworfen
        logger.error(
            "[abo-abschluss] Reservierung %s nicht freigegeben (verfaellt nach %s): %s",
            abschluss_id, RESERVIERUNG_VERFALL, exc,
        )


async def start_subscription_checkout(
    user_id: str, plan_id: str, seats: int, success_redirect: str,
    email: str, name: str, *, consent: "purchase_consent.PurchaseConsentIn",
    ersetzt_abo_id: Optional[str] = None,
) -> Dict[str, str]:
    """Abo-Erstzahlung (Erstkauf oder, mit `ersetzt_abo_id`, Planwechsel).

    Belegt zuerst den einen Abschluss-Platz des Kontos (Migration 070): ein
    zweiter offener Abschluss -> AboAbschlussOffen, ein Erstkauf neben einem
    aktiven Mollie-Abo -> AboSchonAktiv. Das ersetzte Abo wird im Webhook
    gekuendigt, NACHDEM dieses aktiviert ist — nie vorher."""
    # Fail-fast: refuse the Mollie round-trip if the tenant's billing address
    # is incomplete. Without this gate, a user could complete payment on
    # Mollie and then provision_subscription (the webhook handler) would
    # reject the activation because of missing fields — leaving them paid
    # without an active subscription. Routes catch ValueError and translate
    # to 422 with a missing_fields detail, matching the existing webhook
    # error shape. Trial plan does not need this gate, but the FE never
    # checkouts trial; defence-in-depth.
    from src.budget.plans import get_plan

    user_uuid = uuid.UUID(user_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        await _assert_complete_billing_address(conn, user_uuid, plan_id)
    amount = _plan_price(plan_id) * seats
    app_id = get_plan(plan_id).app_id
    alt_uuid = uuid.UUID(ersetzt_abo_id) if ersetzt_abo_id else None

    abschluss_id = await _abschluss_reservieren(user_uuid, app_id, plan_id, seats, alt_uuid)
    try:
        # Zustimmung festhalten, BEVOR die Zahlung entsteht (Migration 054).
        # Diese Bahn erzeugt keine pending_order — order_id bleibt None, die Bahn
        # steht am Beleg.
        async with pool.acquire() as conn:
            await purchase_consent.record_consent(
                conn, consent=consent, user_id=user_id,
                lane=purchase_consent.LANE_SUBSCRIPTION, plan_id=plan_id,
            )

        customer = await get_or_create_customer(user_id, email, name)
        mollie = get_mollie_adapter()
        checkout = await mollie.create_first_payment(
            customer_id=customer["mollieCustomerId"],
            amount_eur=amount,
            description=f"{plan_id} ({seats} Sitz{'e' if seats > 1 else ''})",
            redirect_url=success_redirect,
            webhook_url=config.mollie_webhook_url,
            metadata={
                "type": "subscription_first",
                "userId": user_id,
                "planId": plan_id,
                "seats": str(seats),
                "abschlussId": str(abschluss_id),
            },
        )

        await _abschluss_checkout_eintragen(
            abschluss_id, checkout["paymentId"], user_uuid, plan_id, amount, alt_uuid,
        )
    except BaseException as exc:
        await _abschluss_abbrechen(abschluss_id, f"{type(exc).__name__}: {exc}")
        raise
    return checkout


async def start_project_pack_checkout(
    user_id: str,
    plan_id: str,
    quantity: int,
    success_redirect: str,
    email: str,
    name: str,
    *,
    consent: "purchase_consent.PurchaseConsentIn",
) -> Dict[str, str]:
    """
    Self-Service-Nachbestellung eines Projekt-Pakets via Mollie-Einmalzahlung.

    Nur für BESTANDSKUNDEN (>=1 bereits freigegebene Bestellung) — die
    Erstakquise läuft weiter über die manuelle/partner-geführte Lane. Legt eine
    pending_order (payment_method='mollie', ohne Mahn-Email) an und startet eine
    Mollie-Einmalzahlung. Der Webhook gibt die Order via release_order() frei →
    manual_project_credits (dieselbe Logik wie die Operator-Freigabe).

    Raises:
      ValueError — Plan ist kein Projekt-Plan, quantity < 1, Kunde ist kein
                   Bestandskunde, oder Rechnungsadresse unvollständig.
    """
    from src.budget.plans import require_sellable
    from src.billing import pending_orders_service

    # sellable=false (migration 066, e.g. the former single Energy kind
    # 'energy-project') → PlanNotSellable, a ValueError → HTTP 400.
    plan = require_sellable(plan_id)
    if plan.interval != "project":
        raise ValueError(
            f"plan '{plan_id}' is not a project plan (interval={plan.interval}) — "
            "self-service pack purchase only applies to project plans"
        )
    if quantity < 1:
        raise ValueError("quantity must be >= 1")

    user_uuid = uuid.UUID(user_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        # Bestandskunden-Gate: Self-Service erst nach >=1 freigegebener Bestellung.
        # (So bleibt die Erstakquise bewusst manuell/partner-geführt.)
        is_returning = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pending_orders WHERE user_id = $1 AND status = 'released')",
            user_uuid,
        )
        # Rechnungsadresse vollständig (§11 UStG) — gleiche Regel wie Abos.
        await _assert_complete_billing_address(conn, user_uuid, plan_id)
    if not is_returning:
        raise ValueError(
            "project-pack self-service is only available to returning customers "
            "(no released order yet — the first purchase runs through the "
            "contact/manual lane)"
        )

    # pending_order anlegen — Mollie-Lane, KEINE Mahn-Email (Auto-Release im Webhook).
    order = await pending_orders_service.create_pending_order(
        user_id, plan_id, quantity, send_email=False, payment_method="mollie",
    )
    order_id = str(order["id"])
    # Brutto (Rafael 17.10.2026): Mollie zieht den Rechnungsbetrag ein.
    amount = await _order_invoice_gross(order, expected_net=round(float(plan.price) * quantity, 2))

    # Zustimmung festhalten, BEVOR die Zahlung entsteht (Migration 054).
    # Reihenfolge ist Absicht: schlaegt das Schreiben fehl, gibt es keine
    # Zahlung. Andersherum haetten wir Geld eingenommen und den Beweis verloren.
    async with get_pool().acquire() as _conn:
        await purchase_consent.record_consent(
            _conn, consent=consent, user_id=user_id,
            lane=purchase_consent.LANE_PROJECT_PACK,
            order_id=order_id, plan_id=plan_id, quantity=quantity,
            amount_eur=amount,
        )

    customer = await get_or_create_customer(user_id, email, name)
    mollie = get_mollie_adapter()
    checkout = await mollie.create_one_time_payment(
        customer_id=customer["mollieCustomerId"],
        amount_eur=amount,
        description=f"{plan.name} × {quantity}",
        redirect_url=success_redirect,
        webhook_url=config.mollie_webhook_url,
        metadata={
            "type": "project_pack",
            "userId": user_id,
            "planId": plan_id,
            "quantity": str(quantity),
            "orderId": order_id,
        },
    )

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_payments (payment_id, user_id, type, plan_id, amount_eur, created_at)
            VALUES ($1, $2, 'project_pack', $3, $4, NOW())
            ON CONFLICT (payment_id) DO NOTHING
            """,
            checkout["paymentId"], user_uuid, plan_id, amount,
        )
    return checkout


# Plans allowed through start_first_purchase_pack_checkout — the gate-free
# first-purchase lane. Deliberately explicit allowlist, NOT "any interval=
# 'project' plan": lifting the returning-customer + billing-address gates
# is a per-plan decision, not something that should apply transitively to
# e.g. energy-project just because it shares interval='project'.
FIRST_PURCHASE_PACK_PLAN_IDS = frozenset({
    "report-check-credit",      # 20er-Paket, 5.00 EUR/Check
    "report-check-credit-5",    # 5er-Paket, 7.00 EUR/Check
    "report-check-credit-1",    # Einzelkauf, 9.00 EUR/Check
})

# Feste Kaufmenge je Staffel-Plan. Der Rabatt der Preisstaffel steckt im
# Stückpreis des Plans — ohne festen Mengen-Guard könnte ein Client den
# günstigsten Stückpreis mit beliebiger Menge kaufen (quantity=1 auf dem
# 5-EUR-Plan = Einzel-Check zum 20er-Paketpreis) und die Staffel aushebeln.
FIRST_PURCHASE_PACK_FIXED_QUANTITIES = {
    "report-check-credit": 20,
    "report-check-credit-5": 5,
    "report-check-credit-1": 1,
}

# Plans whose invoice is auto-approved + emailed the moment the Mollie webhook
# releases the order — bypassing INVOICE_REQUIRE_APPROVAL. Deliberately its own
# allowlist, decoupled from FIRST_PURCHASE_PACK_PLAN_IDS above: "gate-free
# first-purchase checkout" and "auto-invoice on payment" are separate business
# decisions that happen to currently coincide for report-check-credit. Rafael
# 2026-07-29: WerkING-Check must invoice fully automatically once marketing is
# live (no manual step in the loop); every other product/lane — B2B
# Rechnungs-Lane, project-pack reorders (e.g. energy-project), subscriptions —
# keeps the manual operator approval in Platform Admin. Do NOT widen this set
# without an explicit per-product decision.
AUTO_SEND_INVOICE_PLAN_IDS = frozenset({
    "report-check-credit",
    "report-check-credit-5",
    "report-check-credit-1",
})

# Kleinbetragsrechnungs-Grenze in Österreich: § 11 Abs 6 UStG 1994 (400 EUR).
# Leitplanke gegen Missbrauch des gate-freien Erstkauf-Pfads für teure Käufe,
# für die die Rechnungsadresspflicht tatsächlich greift — kein Ersatz für
# eine steuerrechtliche Prüfung, falls dieser Wert je erhöht werden soll.
FIRST_PURCHASE_PACK_MAX_AMOUNT_EUR = 400.0


async def start_first_purchase_pack_checkout(
    user_id: str,
    plan_id: str,
    quantity: int,
    success_redirect: str,
    email: str,
    name: str,
    *,
    consent: "purchase_consent.PurchaseConsentIn",
) -> Dict[str, str]:
    """
    Self-Service-ERSTKAUF eines Projekt-Pakets via Mollie-Einmalzahlung.

    Sibling zu start_project_pack_checkout — NICHT diese Funktion verändern.
    Für kleine, klar allowlisted Pakete (initial: report-check-credit), bei
    denen weder das Bestandskunden-Gate noch die volle Rechnungsadresspflicht
    vor dem Mollie-Roundtrip sinnvoll sind: ein Erstkäufer hat per Definition
    noch keine released order, und für Kleinbeträge unter der
    Kleinbetragsrechnungs-Grenze (§ 11 Abs 6 UStG) ist eine vorab vollständige
    Rechnungsadresse keine harte Voraussetzung — _create_order_invoice
    toleriert eine fehlende Adresse bereits heute graceful (billing_address=
    None, Steuersatz-Fallback).

    Unterschiede zu start_project_pack_checkout:
      - KEIN is_returning-Check.
      - KEIN _assert_complete_billing_address-Aufruf.
      - Preis-Cap-Guard (FIRST_PURCHASE_PACK_MAX_AMOUNT_EUR) statt Adresspflicht.
      - Plan-Allowlist (FIRST_PURCHASE_PACK_PLAN_IDS) statt "jeder interval=
        'project'-Plan" — verhindert, dass ein falsch konfigurierter Aufruf
        (z.B. planId=energy-project) versehentlich das Bestandskunden-Gate
        für ein anderes Produkt umgeht.

    metadata.type bleibt "project_pack" (NICHT umbenennen) — handle_webhook
    branched generisch über release_order(order_id), ohne zu prüfen WIE die
    Order entstanden ist; der bestehende Branch funktioniert unverändert.

    Raises:
      ValueError — Plan nicht in der Allowlist, kein Projekt-Plan, quantity
                   < 1, oder Gesamtbetrag über dem Preis-Cap.
    """
    from src.budget.plans import get_plan
    from src.billing import pending_orders_service

    if plan_id not in FIRST_PURCHASE_PACK_PLAN_IDS:
        raise ValueError(
            f"plan '{plan_id}' is not allowlisted for first-purchase self-service "
            f"checkout (allowed: {sorted(FIRST_PURCHASE_PACK_PLAN_IDS)})"
        )

    plan = get_plan(plan_id)
    if plan.interval != "project":
        raise ValueError(
            f"plan '{plan_id}' is not a project plan (interval={plan.interval}) — "
            "first-purchase pack checkout only applies to project plans"
        )
    if quantity < 1:
        raise ValueError("quantity must be >= 1")

    fixed_quantity = FIRST_PURCHASE_PACK_FIXED_QUANTITIES.get(plan_id)
    if fixed_quantity is not None and quantity != fixed_quantity:
        raise ValueError(
            f"plan '{plan_id}' is a fixed-size tier of the Check price ladder "
            f"(quantity must be exactly {fixed_quantity}, got {quantity})"
        )

    amount = round(float(plan.price) * quantity, 2)
    if amount > FIRST_PURCHASE_PACK_MAX_AMOUNT_EUR:
        raise ValueError(
            f"amount EUR {amount} exceeds the first-purchase pack cap of "
            f"EUR {FIRST_PURCHASE_PACK_MAX_AMOUNT_EUR} — larger first purchases "
            "require the invoice-address-gated lane"
        )

    user_uuid = uuid.UUID(user_id)

    # Rechnungsdaten sind beim Kauf PFLICHT (Rafael, 2026-08-05) — ersetzt die
    # Kleinbetrags-Lockerung aus dem Docstring oben ("keine Adresspflicht vor
    # dem Mollie-Roundtrip"): jede Kundenrechnung soll vollstaendige
    # Empfaengerdaten tragen, nicht nur das gesetzliche Minimum der
    # Kleinbetragsrechnung. Gleiches Gate wie Abo-/Pack-Lane (§11 UStG:
    # Name + vollstaendige Adresse; test/internal-Tenants ausgenommen).
    # Fail-fast VOR Order- und Mollie-Anlage — der Fehlertext listet die
    # fehlenden Felder fuer PATCH /v1/tenants/{tid}/billing-address.
    pool = get_pool()
    async with pool.acquire() as conn:
        await _assert_complete_billing_address(conn, user_uuid, plan_id)

    # pending_order anlegen — Mollie-Lane, KEINE Mahn-Email (Auto-Release im Webhook).
    order = await pending_orders_service.create_pending_order(
        user_id, plan_id, quantity, send_email=False, payment_method="mollie",
    )
    order_id = str(order["id"])
    # Brutto (Rafael 17.10.2026): Mollie zieht den Rechnungsbetrag ein. Der
    # Preis-Cap oben bleibt bewusst auf dem Netto (Bestandsregel).
    amount = await _order_invoice_gross(order, expected_net=amount)

    # Zustimmung festhalten, BEVOR die Zahlung entsteht (Migration 054).
    # Reihenfolge ist Absicht: schlaegt das Schreiben fehl, gibt es keine
    # Zahlung. Andersherum haetten wir Geld eingenommen und den Beweis verloren.
    async with get_pool().acquire() as _conn:
        await purchase_consent.record_consent(
            _conn, consent=consent, user_id=user_id,
            lane=purchase_consent.LANE_FIRST_PURCHASE_PACK,
            order_id=order_id, plan_id=plan_id, quantity=quantity,
            amount_eur=amount,
        )

    customer = await get_or_create_customer(user_id, email, name)
    mollie = get_mollie_adapter()
    checkout = await mollie.create_one_time_payment(
        customer_id=customer["mollieCustomerId"],
        amount_eur=amount,
        description=f"{plan.name} × {quantity}",
        redirect_url=success_redirect,
        webhook_url=config.mollie_webhook_url,
        metadata={
            "type": "project_pack",
            "userId": user_id,
            "planId": plan_id,
            "quantity": str(quantity),
            "orderId": order_id,
        },
    )

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_payments (payment_id, user_id, type, plan_id, amount_eur, created_at)
            VALUES ($1, $2, 'project_pack', $3, $4, NOW())
            ON CONFLICT (payment_id) DO NOTHING
            """,
            checkout["paymentId"], user_uuid, plan_id, amount,
        )
    return checkout


async def _abschluss_fuer_zahlung(
    conn: Any, first_payment_id: str, user_uuid: uuid.UUID, app_id: str,
    plan_id: str, seats: int, ersetzt_abo_id: Optional[uuid.UUID],
) -> Optional[Any]:
    """Abschluss-Zeile dieser Erstzahlung, gesperrt (FOR UPDATE).

    Zahlungen von vor Migration 070 haben keine Zeile: sie wird hier
    nachgetragen. Belegt schon ein anderer offener Abschluss den Platz, wird
    sie als 'konflikt' angelegt (Alarm beim Aufrufer)."""
    sql = "SELECT * FROM abo_abschluesse WHERE payment_id = $1 FOR UPDATE"
    ab = await conn.fetchrow(sql, first_payment_id)
    if ab:
        return ab
    try:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO abo_abschluesse
                     (user_id, app_id, plan_id, seats, alt_abo_id, payment_id, status)
                   VALUES ($1, $2::app_id, $3::plan_id, $4, $5, $6, 'offen')
                   ON CONFLICT (payment_id) DO NOTHING""",
                user_uuid, app_id, plan_id, seats, ersetzt_abo_id, first_payment_id,
            )
    except asyncpg.UniqueViolationError:
        await conn.execute(
            """INSERT INTO abo_abschluesse
                 (user_id, app_id, plan_id, seats, alt_abo_id, payment_id, status, letzter_fehler)
               VALUES ($1, $2::app_id, $3::plan_id, $4, $5, $6, 'konflikt',
                       'Erstzahlung ohne Abschluss, Platz von anderem Abschluss belegt')
               ON CONFLICT (payment_id) DO NOTHING""",
            user_uuid, app_id, plan_id, seats, ersetzt_abo_id, first_payment_id,
        )
    return await conn.fetchrow(sql, first_payment_id)


_ABO_SPALTEN = """id, user_id, app_id, plan_id, status, mollie_customer_id,
                  mollie_subscription_id, seats, started_at, cancelled_at"""


async def _activate_subscription(
    user_id: str, plan_id: str, seats: int, customer_id: str,
    first_payment_id: str, ersetzt_abo_id: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """
    Aktiviert das Abo einer bezahlten Erstzahlung — genau einmal, auch bei
    gleichzeitigen Webhooks und nach Abstuerzen (Codex-Befund 4, 18.10.2026).

    Unter der Zeilensperre des Abschlusses (abo_abschluesse, FOR UPDATE):
      - schon aktiviert -> vorhandene Zeile zurueck, Mollie wird nicht gerufen;
      - sonst Mollie-Abo suchen (metadata.firstPaymentId) oder mit
        Idempotenzschluessel anlegen, Zeile schreiben, Abschluss fortschreiben.
    Ein zweiter Webhook wartet auf die Sperre und sieht danach die Zeile.

    Wuerde die Aktivierung die Invariante verletzen (anderes aktives
    Mollie-Abo im Konto, Abschluss schon abgebrochen), wird nichts angelegt:
    Abschluss -> 'konflikt', Alarm, AboAbschlussKonflikt.

    Provisions the plan's monthly budget pot alongside the row (see
    _ensure_monthly_budget_pot) — this is the real paid-checkout path, so a
    subscription activated here without a pot is the exact "Kein KI-Guthaben
    despite active paid plan" bug the helper exists to prevent.
    """
    from src.budget.plans import get_plan

    pool = get_pool()
    app_id = get_plan(plan_id).app_id
    user_uuid = uuid.UUID(user_id)
    amount = _plan_price(plan_id) * seats
    angelegt = None
    konflikt: Optional[str] = None
    konflikt_neu = False

    async with pool.acquire() as conn:
        # Aktivierung von vor Migration 070 (alter Code, kein Abschluss):
        # unveraendert zurueckgeben.
        bestand = await conn.fetchrow(
            f"SELECT {_ABO_SPALTEN} FROM subscriptions WHERE mollie_first_payment_id = $1",
            first_payment_id,
        )
        hat_abschluss = await conn.fetchval(
            "SELECT 1 FROM abo_abschluesse WHERE payment_id = $1", first_payment_id,
        )
        if bestand and not hat_abschluss:
            await _ensure_monthly_budget_pot(conn, user_uuid, plan_id)
            return _serialize_subscription(bestand)

        async with conn.transaction():
            ab = await _abschluss_fuer_zahlung(
                conn, first_payment_id, user_uuid, app_id, plan_id, seats, ersetzt_abo_id,
            )
            if ab["user_id"] != user_uuid:
                raise AboAbschlussKonflikt(
                    f"Erstzahlung {first_payment_id}: Abschluss gehoert {ab['user_id']}, nicht {user_id}"
                )
            if ab["status"] in ("neu_aktiv", "doppelt_aktiv", "abgeschlossen"):
                # Wiederholung: aktiviert ist schon. Mollie NICHT erneut rufen.
                row = await conn.fetchrow(
                    f"SELECT {_ABO_SPALTEN} FROM subscriptions WHERE id = $1", ab["neu_abo_id"],
                )
                await _ensure_monthly_budget_pot(conn, user_uuid, plan_id)
                return _serialize_subscription(row)

            if ab["status"] != "offen":
                konflikt = f"Abschluss {ab['id']} steht auf '{ab['status']}'"
            else:
                alt_id = ab["alt_abo_id"]
                alt = None
                if alt_id is not None:
                    alt = await conn.fetchrow(
                        "SELECT id, status, mollie_subscription_id FROM subscriptions WHERE id = $1 FOR UPDATE",
                        alt_id,
                    )
                andere = [r["id"] for r in await conn.fetch(
                    """SELECT id FROM subscriptions
                        WHERE user_id = $1 AND app_id = $2::app_id AND status = 'active'
                          AND mollie_subscription_id IS NOT NULL
                        FOR UPDATE""",
                    user_uuid, app_id,
                ) if r["id"] != alt_id and (bestand is None or r["id"] != bestand["id"])]
                if andere:
                    konflikt = f"weiteres aktives Mollie-Abo {andere} im Konto"
                else:
                    row = bestand
                    if row is None:
                        mollie = get_mollie_adapter()
                        mollie_sub = await mollie.find_subscription_for_first_payment(
                            customer_id, first_payment_id,
                        )
                        if mollie_sub is None:
                            resp = await mollie.create_subscription(
                                customer_id=customer_id,
                                amount_eur=amount,
                                interval="1 month",
                                description=f"{plan_id} ({seats} Sitz{'e' if seats > 1 else ''})",
                                webhook_url=config.mollie_webhook_url,
                                metadata={
                                    "userId": user_id, "planId": plan_id,
                                    "firstPaymentId": first_payment_id,
                                    "abschlussId": str(ab["id"]),
                                },
                                idempotency_key=f"abo-erstzahlung-{first_payment_id}",
                            )
                            mollie_sub = resp["subscriptionId"]
                        else:
                            logger.warning(
                                "[abo-abschluss] Mollie-Abo %s zu Erstzahlung %s gefunden statt neu angelegt",
                                mollie_sub, first_payment_id,
                            )
                        ueberlappt = (
                            alt_id if alt is not None and alt["status"] == "active"
                            and alt["mollie_subscription_id"] else None
                        )
                        row = await conn.fetchrow(
                            f"""
                            INSERT INTO subscriptions
                              (id, user_id, app_id, plan_id, status,
                               mollie_customer_id, mollie_subscription_id, mollie_first_payment_id,
                               seats, started_at, ueberlappt_abo_id)
                            VALUES ($1, $2, $3, $4, 'active', $5, $6, $7, $8, NOW(), $9)
                            RETURNING {_ABO_SPALTEN}
                            """,
                            uuid.uuid4(), user_uuid, app_id, plan_id,
                            customer_id, mollie_sub, first_payment_id, seats, ueberlappt,
                        )
                        angelegt = row
                    await _ensure_monthly_budget_pot(conn, user_uuid, plan_id)
                    alt_laeuft = alt is not None and alt["status"] in ("active", "suspended")
                    await conn.execute(
                        """UPDATE abo_abschluesse SET neu_abo_id = $2, status = $3, ergebnis = $4,
                                  geaendert_am = NOW()
                            WHERE id = $1""",
                        ab["id"], row["id"],
                        "neu_aktiv" if alt_laeuft else "abgeschlossen",
                        None if alt_laeuft else ("erstkauf" if alt_id is None else "alt_schon_beendet"),
                    )

            if konflikt:
                # Ereignis genau einmal je Zahlung — auch wenn die Zeile schon
                # als 'konflikt' angelegt wurde (Altbestand ohne freien Platz).
                konflikt_neu = not await conn.fetchval(
                    """SELECT 1 FROM billing_events
                        WHERE event_type = 'subscription.activation_conflict'
                          AND mollie_payment_id = $1""",
                    first_payment_id,
                )
                await conn.execute(
                    """UPDATE abo_abschluesse SET status = 'konflikt', letzter_fehler = $2,
                              geaendert_am = NOW()
                        WHERE id = $1""",
                    ab["id"], konflikt,
                )

    if konflikt:
        logger.error(
            "[ALARM][abo-abschluss] bezahlte Erstzahlung %s (Nutzer %s, %s) NICHT aktiviert: %s",
            first_payment_id, user_id, plan_id, konflikt,
        )
        if konflikt_neu:
            await log_billing_event(
                "subscription.activation_conflict",
                user_id=user_id, mollie_payment_id=first_payment_id,
                amount_eur=float(amount), source="mollie-webhook",
                payload={"planId": plan_id, "seats": seats, "grund": konflikt},
            )
        raise AboAbschlussKonflikt(konflikt)

    if angelegt is not None:
        # Nach dem Commit: billing_events/invoices haengen per FK an der neuen
        # Zeile (zweite Verbindung, siehe cancel_subscription).
        await log_billing_event(
            "subscription.activated",
            user_id=user_id,
            subscription_id=str(angelegt["id"]),
            mollie_payment_id=first_payment_id,
            amount_eur=float(amount),
            source="mollie-webhook",
            payload={"planId": plan_id, "seats": seats},
        )
        await auto_create_invoice(
            user_id=user_id,
            amount_eur=float(amount),
            description=f"{plan_id} — Erste Monatsabrechnung ({seats} Sitz" + ("e" if seats > 1 else "") + ")",
            subscription_id=str(angelegt["id"]),
            mollie_payment_id=first_payment_id,
            number_prefix=prefix_for_app(app_id),
        )
    return _serialize_subscription(row)


async def _wechsel_abschliessen(first_payment_id: str) -> Dict[str, Any]:
    """Kuendigt das im Wechsel gemerkte alte Abo — nur wenn das neue aktiv ist.

    Unter Sperre von Abschluss, neuem und altem Abo:
      - altes laeuft nicht mehr       -> abgeschlossen (alt_schon_beendet)
      - neues nicht (mehr) aktiv      -> abgeschlossen (ersatz_nicht_aktiv),
        altes bleibt: nie das letzte Abo kuendigen (Codex-Befund 3)
      - sonst Mollie-Kuendigung; Erfolg -> abgeschlossen (alt_gekuendigt),
        Fehler -> 'doppelt_aktiv' + ALARM, AboDoppeltAktiv (Codex-Befund 2).
    Wiederholbar: Webhook-Wiederholung und Nachholauftrag rufen dasselbe.
    """
    pool = get_pool()
    ergebnis: Dict[str, Any] = {}
    fehler: Optional[str] = None
    erster_alarm = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            ab = await conn.fetchrow(
                "SELECT * FROM abo_abschluesse WHERE payment_id = $1 FOR UPDATE", first_payment_id,
            )
            if ab is None or ab["status"] not in ("neu_aktiv", "doppelt_aktiv"):
                return {
                    "status": ab["status"] if ab else None,
                    "ergebnis": ab["ergebnis"] if ab else None,
                    "altAboId": str(ab["alt_abo_id"]) if ab and ab["alt_abo_id"] else None,
                }
            neu = await conn.fetchrow(
                "SELECT status FROM subscriptions WHERE id = $1 FOR UPDATE", ab["neu_abo_id"],
            )
            alt = await conn.fetchrow(
                """SELECT status, mollie_customer_id, mollie_subscription_id
                     FROM subscriptions WHERE id = $1 FOR UPDATE""",
                ab["alt_abo_id"],
            )
            ergebnis = {"altAboId": str(ab["alt_abo_id"]), "neuAboId": str(ab["neu_abo_id"]),
                        "userId": str(ab["user_id"])}
            if alt["status"] not in ("active", "suspended"):
                ausgang = "alt_schon_beendet"
            elif neu["status"] != "active":
                ausgang = "ersatz_nicht_aktiv"
            else:
                try:
                    if alt["mollie_subscription_id"]:
                        await get_mollie_adapter().cancel_subscription(
                            alt["mollie_customer_id"], alt["mollie_subscription_id"],
                        )
                except Exception as exc:  # noqa: BLE001 — wird als doppelt_aktiv festgehalten und weitergeworfen
                    fehler = f"{type(exc).__name__}: {exc}"
                    ausgang = None
                else:
                    await conn.execute(
                        "UPDATE subscriptions SET status='cancelled', cancelled_at=NOW() WHERE id=$1",
                        ab["alt_abo_id"],
                    )
                    ausgang = "alt_gekuendigt"
            if fehler is not None:
                erster_alarm = ab["status"] != "doppelt_aktiv"
                zeile = await conn.fetchrow(
                    """UPDATE abo_abschluesse
                          SET status = 'doppelt_aktiv', letzter_fehler = $2,
                              kuendigung_versuche = kuendigung_versuche + 1,
                              doppelt_aktiv_seit = COALESCE(doppelt_aktiv_seit, NOW()),
                              geaendert_am = NOW()
                        WHERE id = $1
                    RETURNING kuendigung_versuche, doppelt_aktiv_seit""",
                    ab["id"], fehler[:2000],
                )
                ergebnis.update(status="doppelt_aktiv", ergebnis=None,
                                versuche=zeile["kuendigung_versuche"],
                                doppeltAktivSeit=zeile["doppelt_aktiv_seit"].isoformat())
            else:
                await conn.execute(
                    """UPDATE abo_abschluesse
                          SET status = 'abgeschlossen', ergebnis = $2::varchar, geaendert_am = NOW(),
                              kuendigung_versuche = kuendigung_versuche
                                  + CASE WHEN $2::varchar = 'alt_gekuendigt' THEN 1 ELSE 0 END
                        WHERE id = $1""",
                    ab["id"], ausgang,
                )
                ergebnis.update(status="abgeschlossen", ergebnis=ausgang,
                                warDoppeltAktiv=ab["status"] == "doppelt_aktiv")

    uid = ergebnis["userId"]
    if fehler is not None:
        logger.error(
            "[ALARM][abo-wechsel] Kuendigung von Abo %s gescheitert (Versuch %s) — altes UND neues "
            "Abo %s laufen seit %s, Abbuchungen des alten Abos ab dann sind zu erstatten: %s",
            ergebnis["altAboId"], ergebnis["versuche"], ergebnis["neuAboId"],
            ergebnis["doppeltAktivSeit"], fehler,
        )
        if erster_alarm:
            await log_billing_event(
                "subscription.double_active", user_id=uid,
                subscription_id=ergebnis["altAboId"], mollie_payment_id=first_payment_id,
                source="plan-change",
                payload={"neuAboId": ergebnis["neuAboId"], "fehler": fehler,
                         "seit": ergebnis["doppeltAktivSeit"]},
            )
        raise AboDoppeltAktiv(
            f"Abo {ergebnis['altAboId']} nicht gekuendigt, {ergebnis['neuAboId']} aktiv: {fehler}"
        )
    if ergebnis["ergebnis"] == "alt_gekuendigt":
        await log_billing_event(
            "subscription.cancelled", user_id=uid, subscription_id=ergebnis["altAboId"],
            mollie_payment_id=first_payment_id, source="plan-change",
            payload={"ersetztDurch": ergebnis["neuAboId"]},
        )
    elif ergebnis["ergebnis"] == "ersatz_nicht_aktiv":
        logger.warning(
            "[abo-wechsel] Ersatz %s nicht aktiv — altes Abo %s bleibt bestehen",
            ergebnis["neuAboId"], ergebnis["altAboId"],
        )
        await log_billing_event(
            "subscription.change_kept_old", user_id=uid, subscription_id=ergebnis["altAboId"],
            mollie_payment_id=first_payment_id, source="plan-change",
            payload={"neuAboId": ergebnis["neuAboId"]},
        )
    if ergebnis.get("warDoppeltAktiv"):
        await log_billing_event(
            "subscription.double_active_resolved", user_id=uid,
            subscription_id=ergebnis["altAboId"], mollie_payment_id=first_payment_id,
            source="plan-change", payload={"ergebnis": ergebnis["ergebnis"]},
        )
    return ergebnis


async def wechsel_kuendigungen_nachholen() -> Dict[str, Any]:
    """Nachholauftrag: jeder Wechsel in 'neu_aktiv'/'doppelt_aktiv' wird erneut
    abgeschlossen. Fehler bleiben je Wechsel sichtbar (Alarm, Status), der Lauf
    geht weiter."""
    pool = get_pool()
    async with pool.acquire() as conn:
        zahlungen = [r["payment_id"] for r in await conn.fetch(
            """SELECT payment_id FROM abo_abschluesse
                WHERE status IN ('neu_aktiv', 'doppelt_aktiv') ORDER BY geaendert_am"""
        )]
    bericht: Dict[str, Any] = {"geprueft": len(zahlungen), "abgeschlossen": 0, "doppeltAktiv": []}
    for pid in zahlungen:
        try:
            erg = await _wechsel_abschliessen(pid)
        except AboDoppeltAktiv:
            bericht["doppeltAktiv"].append(pid)
            continue
        if erg.get("status") == "abgeschlossen":
            bericht["abgeschlossen"] += 1
    return bericht


async def list_subscriptions(user_id: str) -> List[Dict[str, Any]]:
    """
    Returns the user's subscriptions, lazy-expiring any active trial whose
    trial_ends_at has passed. Lazy because:
      * Forced-trial cohorts are small (one row per registered user) so the
        UPDATE-on-read overhead is negligible vs. running a cron-job.
      * Lazy keeps the expiry state correct *for the caller's view* even
        if a backstop scheduler missed a window — no stale UI possible.
    A scheduled job would still be a useful belt-and-suspenders defense for
    apps that don't call list_subscriptions on every request (planned: see
    follow-up issue). For now this is the single load-bearing expire path.

    The UPDATE runs in a separate transaction-friendly statement BEFORE
    the SELECT so the returned rows already reflect the new state. A
    concurrent caller racing the same UPDATE is safe: the WHERE clause
    re-matches only rows still in the past-due state, the second writer
    just no-ops.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # Lazy expiry — UPDATE first so the SELECT below sees the new state.
        await conn.execute(
            """
            UPDATE subscriptions
            SET status = 'expired'::subscription_status,
                expired_at = NOW()
            WHERE user_id = $1
              AND plan_id = 'trial'
              AND status = 'active'
              AND trial_ends_at IS NOT NULL
              AND trial_ends_at < NOW()
            """,
            uuid.UUID(user_id),
        )
        rows = await conn.fetch(
            """
            SELECT id, user_id, app_id, plan_id, status, mollie_customer_id, mollie_subscription_id,
                   seats, started_at, cancelled_at, suspended_at, expired_at, trial_ends_at
            FROM subscriptions WHERE user_id = $1 ORDER BY started_at DESC
            """,
            uuid.UUID(user_id),
        )
    return [_serialize_subscription(r) for r in rows]


# Matches the registration forced-trial window (identity/routes.py register:
# `trial_ends_at = now + INTERVAL '7 days'`). One constant, one semantic.
ADMIN_GRANT_TRIAL_DAYS = 7


async def _ensure_monthly_budget_pot(conn: Any, user_uuid: uuid.UUID, plan_id: str) -> None:
    """Provision plan_id's monthly budget pot if this plan requires one — idempotent.

    The single gate every subscription creation/activation path calls once a
    row is written (or found) with status='active' — an active subscription
    without a budget pot is access on paper only: the app renders "Kein
    KI-Guthaben" and every AI call is refused, while login and /dashboard stay
    green (the failure hides one layer below every check an operator runs).
    Until 2026-08-26 only the seeding path (provision_subscription) provisioned
    the pot on its insert branch — grant_subscription, provision_subscription's
    idempotent-return branch, and the real Mollie activation path
    (_activate_subscription) all produced that silent dud.

    No-ops for two cases where the absence of a pot is the right state:
      * trial plans — their reset_at doubles as the expiry date, so writing a
        30-day anchor here would silently stretch the trial. They provision
        themselves in evaluate_budget via _provision_trial.
      * interval != 'month' (today energy-project) — those pots are per-project
        and live in project_budgets_service, keyed by project_id. A monthly
        lane would sit there unread — see budget/routes.py:_require_month_interval.

    _provision_plan_budget only inserts when the lane is absent, so calling
    this against an already-active subscription safely repairs a missing pot
    without ever resetting a live one — that's what makes it safe to call on
    every idempotent "subscription already exists" return path, not just on
    fresh inserts.

    An unknown plan_id propagates get_plan's ValueError. The plans catalog is
    the SSoT for what can be sold; a plan we cannot price is one we cannot
    budget, and the resulting subscription could never serve an AI call
    (evaluate_budget would raise on it too) — failing here surfaces that at
    grant/activation time instead of at the customer's first prompt.
    """
    from src.budget.plans import get_plan

    plan = get_plan(plan_id)  # raises ValueError for unknown plan
    if plan.trial or plan.interval != "month":
        return
    await _provision_plan_budget(conn, user_uuid, plan)


async def grant_subscription(
    user_id: str,
    app_id: str,
    plan_id: str,
    seats: int = 1,
    granted_by: str = "operator",
) -> Tuple[Dict[str, Any], bool]:
    """
    Operator grant: create an ACTIVE subscription without a Mollie checkout.

    The admin-side twin of the Mollie activation path — and the ONLY
    sanctioned way to hand out access outside a payment flow (real-customer
    onboarding, demos, support). It exists because entitlements — the claim
    every app gates on — derive exclusively from this table: an app_license
    alone grants NOTHING. (2026-07-06: a real customer bounced with
    reason=no-license while holding a valid energy license, and the fix
    required hand-written SQL against the prod DB. Never again.)

    Semantics (defensive, fail fast):
      * Caller validates app_id/plan_id against the enum allowlists and maps
        errors to HTTP — this function trusts its inputs and lets Postgres
        reject anything that slips through (enum cast raises, no fallback).
      * If the user already holds an ACTIVE subscription for the app, that
        row is returned unchanged with created=False. Billing rows are never
        silently mutated — changing a live subscription is a deliberate,
        separate operation (cancel + re-grant).
      * plan 'trial' → trial_ends_at = now + ADMIN_GRANT_TRIAL_DAYS and
        mollie_customer_id stays NULL (the schema CHECK allows NULL only for
        trials — list_subscriptions lazy-expires the row past its window).
      * any other plan → no expiry; the CHECK demands a non-NULL
        mollie_customer_id although no Mollie customer exists, so we write
        the established seed marker 'seed-<user_id>' (the same convention the
        2026-06-18 customer seeding used) — seeded and admin-granted rows
        stay queryable as one class.
      * metadata records the honest provenance: the seed marker satisfies
        the constraint, the metadata says WHY the row exists.
      * the plan's monthly budget pot is provisioned with the row, and a
        'subscription.granted' event is appended to the audit trail. Both were
        missing until 2026-08-26 — see _ensure_monthly_budget_pot for which
        plans get a pot and why the others must not.

    Returns (serialized_subscription, created).
    Raises ValueError on a malformed user_id (caller maps to 400).
    """
    uid = uuid.UUID(user_id)
    now = datetime.now(timezone.utc)
    is_trial = plan_id == "trial"
    trial_ends_at = now + timedelta(days=ADMIN_GRANT_TRIAL_DAYS) if is_trial else None
    mollie_customer_id = None if is_trial else f"seed-{user_id}"
    metadata = json.dumps({"source": "admin-grant", "granted_by": granted_by})

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Idempotency probe inside the transaction. Two racing granters
            # can at worst insert two active rows — entitlement semantics
            # tolerate that (ANY active row passes, see identity routes'
            # _entitlements_for) — the probe keeps the common path single-row.
            existing = await conn.fetchrow(
                """
                SELECT id, user_id, app_id, plan_id, status, mollie_customer_id,
                       mollie_subscription_id, seats, started_at, cancelled_at,
                       suspended_at, expired_at, trial_ends_at
                  FROM subscriptions
                 WHERE user_id = $1
                   AND app_id = $2::app_id
                   AND status = 'active'::subscription_status
                 ORDER BY started_at DESC
                 LIMIT 1
                """,
                uid,
                app_id,
            )
            if existing:
                # Self-heal, mirroring provision_subscription's license repair:
                # a re-grant tops up a pot that is missing on an already-active
                # subscription (every grant issued before 2026-08-26 left one
                # behind).
                await _ensure_monthly_budget_pot(conn, uid, existing["plan_id"])
                return _serialize_subscription(existing), False

            row = await conn.fetchrow(
                """
                INSERT INTO subscriptions
                    (user_id, app_id, plan_id, status, mollie_customer_id,
                     seats, started_at, trial_ends_at, metadata)
                VALUES
                    ($1, $2::app_id, $3::plan_id, 'active'::subscription_status,
                     $4, $5, $6, $7, $8::jsonb)
                RETURNING id, user_id, app_id, plan_id, status, mollie_customer_id,
                          mollie_subscription_id, seats, started_at, cancelled_at,
                          suspended_at, expired_at, trial_ends_at
                """,
                uid,
                app_id,
                plan_id,
                mollie_customer_id,
                seats,
                now,
                trial_ends_at,
                metadata,
            )

            # A subscription without a budget pot is access on paper only: the
            # app renders "Kein KI-Guthaben" and every AI call is refused, while
            # login and /dashboard stay green — the failure hides one layer below
            # every check an operator runs. Until 2026-08-26 only the seeding path
            # (provision_subscription) provisioned the pot, so admin grants for a
            # paid monthly plan produced exactly that silent dud.
            #
            # Trials are excluded on purpose: for a trial, monthly_budgets.reset_at
            # IS the expiry date (_is_trial_expired reads it), and the 30-day anchor
            # written here would silently stretch a 7-day trial. They provision
            # themselves in evaluate_budget via _provision_trial.
            #
            # Same connection as the INSERT: pot and subscription commit together
            # or not at all.
            await _ensure_monthly_budget_pot(conn, uid, plan_id)

    # After COMMIT, never inside it: log_billing_event writes on a second pooled
    # connection and its FK to subscriptions(id) cannot see an uncommitted row.
    await log_billing_event(
        "subscription.granted",
        user_id=user_id,
        subscription_id=_serialize_subscription(row)["id"],
        source="admin",
        payload={
            "planId": plan_id,
            "appId": app_id,
            "seats": seats,
            "grantedBy": granted_by,
        },
    )
    return _serialize_subscription(row), True


async def cancel_subscription(user_id: str, subscription_id: str) -> None:
    """
    Cancel a subscription transactionally.

    The Mollie cancel call and the DB UPDATE run inside a single
    transaction that holds FOR UPDATE on the subscription row. If the
    Mollie call raises, the transaction rolls back and the DB state
    remains 'active' — Mollie will continue charging, but admin views
    will not lie about the status. Concurrent cancel requests serialise
    on the row lock; the second one observes status='cancelled' and
    becomes a no-op.

    Failure mode we explicitly accept: Mollie cancel succeeds, then the
    DB commit fails (e.g. connection drop). Mollie stops charging, DB
    keeps 'active'. We treat that as a recoverable inconsistency — an
    admin retry of cancel finds Mollie already cancelled (idempotent on
    Mollie's side) and the DB UPDATE completes. The opposite ordering
    (DB cancel, Mollie still charges) would be a billing leak; we forbid
    it by calling Mollie BEFORE the DB UPDATE inside the transaction.
    """
    pool = get_pool()
    sub_uuid = uuid.UUID(subscription_id)
    user_uuid = uuid.UUID(user_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT mollie_customer_id, mollie_subscription_id, status
                FROM subscriptions WHERE id = $1 AND user_id = $2 FOR UPDATE
                """,
                sub_uuid, user_uuid,
            )
            if not row:
                raise LookupError(
                    f"Subscription {subscription_id} not found for user {user_id}"
                )
            if row["status"] == "cancelled":
                # Already cancelled — concurrent request lost the race. No-op.
                return

            if row["mollie_subscription_id"]:
                mollie = get_mollie_adapter()
                await mollie.cancel_subscription(
                    row["mollie_customer_id"], row["mollie_subscription_id"]
                )

            await conn.execute(
                "UPDATE subscriptions SET status='cancelled', cancelled_at=NOW() WHERE id=$1",
                sub_uuid,
            )

    # Log AFTER the transaction commits — NOT inside it. billing_events has an
    # FK to subscriptions(id); log_billing_event inserts on a SECOND pooled
    # connection, whose FK check needs a KEY SHARE lock on the very row this
    # transaction just UPDATEd (and still holds FOR UPDATE). The two connections
    # wait on each other, Postgres can't see a deadlock (our txn is idle-in-
    # transaction, not waiting on a DB resource), so it stalls until asyncpg's
    # command timeout → the whole cancel rolls back and silently never lands.
    # This mirrors the suspend/expire paths below, which already log post-commit.
    await log_billing_event(
        "subscription.cancelled",
        user_id=user_id,
        subscription_id=subscription_id,
        source="admin",
    )


# ---------------------------------------------------------------------------
# Top-Up (gemeinsamer Pool!)
# ---------------------------------------------------------------------------

async def start_topup_checkout(
    user_id: str, amount_eur: float, success_redirect: str,
    email: str, name: str, *, consent: "purchase_consent.PurchaseConsentIn",
) -> Dict[str, str]:
    if amount_eur < 50 or amount_eur > 1000:
        raise ValueError(f"Top-Up amount EUR {amount_eur} out of range [50, 1000]")

    # Zustimmung festhalten, BEVOR die Zahlung entsteht (Migration 054).
    # Diese Bahn erzeugt keine pending_order — order_id bleibt None, die Bahn
    # steht am Beleg.
    async with get_pool().acquire() as _conn:
        await purchase_consent.record_consent(
            _conn, consent=consent, user_id=user_id,
            lane=purchase_consent.LANE_TOPUP, amount_eur=amount_eur,
        )

    customer = await get_or_create_customer(user_id, email, name)
    mollie = get_mollie_adapter()
    checkout = await mollie.create_one_time_payment(
        customer_id=customer["mollieCustomerId"],
        amount_eur=amount_eur,
        description=f"API-Top-Up EUR {amount_eur}",
        redirect_url=success_redirect,
        webhook_url=config.mollie_webhook_url,
        metadata={"type": "topup", "userId": user_id},
    )

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_payments (payment_id, user_id, type, amount_eur, created_at)
            VALUES ($1, $2, 'topup', $3, NOW())
            ON CONFLICT (payment_id) DO NOTHING
            """,
            checkout["paymentId"], uuid.UUID(user_id), amount_eur,
        )
    return checkout


async def _credit_topup(user_id: str, amount_eur: float, mollie_payment_id: str) -> Dict[str, Any]:
    pool = get_pool()
    user_uuid = uuid.UUID(user_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            cust = await conn.fetchrow(
                "SELECT mollie_customer_id FROM mollie_customers WHERE user_id = $1",
                user_uuid,
            )
            if not cust:
                raise LookupError(f"No Mollie customer for user {user_id}")

            # Idempotency: wenn schon credit_purchase fuer payment_id existiert, skip
            existing = await conn.fetchval(
                "SELECT id FROM credit_purchases WHERE mollie_payment_id = $1",
                mollie_payment_id,
            )
            # Sichtbarer TopUp-Saldo = Summe der aktiven (nicht abgelaufenen) Lots.
            _active_balance_sql = (
                "SELECT COALESCE(SUM(amount_eur), 0) FROM user_topup_lots "
                "WHERE user_id = $1 AND expires_at > NOW() AND amount_eur > 0"
            )
            if existing:
                # Hole aktuellen Balance ohne nochmal zu erhoehen
                balance = await conn.fetchval(_active_balance_sql, user_uuid)
                return {"alreadyCredited": True, "balanceEur": float(balance or 0)}

            purchase_id = uuid.uuid4()
            paid_at = await conn.fetchval(
                """
                INSERT INTO credit_purchases (id, user_id, pack_eur, mollie_customer_id, mollie_payment_id, paid_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                RETURNING paid_at
                """,
                purchase_id, user_uuid, amount_eur, cust["mollie_customer_id"], mollie_payment_id,
            )
            # TopUp als datiertes Lot (Budget-Modell): purchased_at = realer
            # Kaufzeitpunkt (credit_purchases.paid_at), expires_at = +12 Monate.
            # Kein erfundenes Datum — beides aus dem Kauf abgeleitet.
            #
            # `$3::timestamptz` ist Pflicht: ohne Typ leitet Postgres aus
            # `$3 + INTERVAL` fuer $3 den Typ interval ab und aus der Spalte
            # purchased_at timestamptz → AmbiguousParameterError, die
            # Transaktion rollt zurueck, der Webhook antwortet 500. So ist
            # seit e993d0e (05.07.) JEDE Aufladung verloren gegangen (gemessen
            # 23.09. an tr_bDcNDGUB3ax8Atso6kBXJ, dev-Bridge). Die Tests mit
            # nachgebautem Pool konnten das nicht sehen.
            await conn.execute(
                """
                INSERT INTO user_topup_lots
                    (user_id, amount_eur, purchased_at, expires_at, credit_purchase_id)
                VALUES ($1, $2, $3::timestamptz, $3::timestamptz + INTERVAL '12 months', $4)
                """,
                user_uuid, amount_eur, paid_at, purchase_id,
            )
            new_balance = await conn.fetchval(_active_balance_sql, user_uuid)
    await log_billing_event(
        "topup.credited",
        user_id=user_id,
        mollie_payment_id=mollie_payment_id,
        amount_eur=amount_eur,
        source="mollie-webhook",
        payload={"newBalanceEur": float(new_balance)},
    )
    await auto_create_invoice(
        user_id=user_id,
        amount_eur=amount_eur,
        description=f"API-Top-Up € {amount_eur:.2f}",
        mollie_payment_id=mollie_payment_id,
        number_prefix=TOPUP_PREFIX,
    )
    return {"alreadyCredited": False, "balanceEur": float(new_balance)}


async def list_credit_purchases(user_id: str) -> List[Dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, pack_eur, mollie_customer_id, mollie_payment_id, paid_at
            FROM credit_purchases WHERE user_id = $1 ORDER BY paid_at DESC
            """,
            uuid.UUID(user_id),
        )
    return [
        {
            "id": str(r["id"]),
            "userId": str(r["user_id"]),
            "amountEur": float(r["pack_eur"]),
            "mollieCustomerId": r["mollie_customer_id"],
            "molliePaymentId": r["mollie_payment_id"],
            "paidAt": r["paid_at"].isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Webhook (Mollie ruft auf)
# ---------------------------------------------------------------------------

async def _record_failed_payment(payment_id: str, status: str) -> None:
    """Append a billing_event for a Mollie payment that will never complete.

    No state rollback is needed — subscription / topup activation only ever
    runs on status == "paid" — but a lost payment must stay auditable.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        pending = await conn.fetchrow(
            "SELECT user_id, type, plan_id, amount_eur FROM pending_payments WHERE payment_id = $1",
            payment_id,
        )
    if not pending:
        # Not one of ours, or the pending row is already gone — nothing to attribute.
        return
    await log_billing_event(
        "payment.failed",
        user_id=str(pending["user_id"]),
        mollie_payment_id=payment_id,
        amount_eur=float(pending["amount_eur"]) if pending["amount_eur"] is not None else None,
        source="mollie-webhook",
        payload={"status": status, "type": pending["type"], "planId": pending["plan_id"]},
    )


async def _suspend_subscription_by_mollie_id(
    mollie_subscription_id: str,
    payment_id: str,
    status: str,
) -> None:
    """Suspend an active subscription when its recurring Mollie payment fails.

    Mollie fires the webhook for each payment in a subscription — including
    recurring monthly ones.  Those payments are NOT in pending_payments (only
    the first payment is), so handle_webhook falls through here.  We look up
    the subscription by mollie_subscription_id; if it is 'active' we flip it
    to 'suspended' and leave an audit trail.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE subscriptions
               SET status = 'suspended', suspended_at = NOW()
             WHERE mollie_subscription_id = $1 AND status = 'active'
            RETURNING id, user_id
            """,
            mollie_subscription_id,
        )
    if row:
        await log_billing_event(
            "subscription.suspended",
            user_id=str(row["user_id"]),
            subscription_id=str(row["id"]),
            mollie_payment_id=payment_id,
            source="mollie-webhook",
            payload={"reason": f"recurring_payment_{status}"},
        )


async def expire_subscription_for_user_plan(user_id: str, plan_id: str) -> None:
    """Mark the user's active subscription for plan_id as 'expired'.

    Called when a trial's budget window closes (trial_expired in evaluate_budget).
    A no-op when no active subscription row exists (trials provisioned purely
    through user_budgets have no subscription row).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE subscriptions
               SET status = 'expired', expired_at = NOW()
             WHERE user_id = $1 AND plan_id = $2::plan_id AND status = 'active'
            RETURNING id
            """,
            uuid.UUID(user_id),
            plan_id,
        )
    if row:
        await log_billing_event(
            "subscription.expired",
            user_id=user_id,
            subscription_id=str(row["id"]),
            source="budget-check",
            payload={"planId": plan_id, "reason": "trial_period_ended"},
        )


async def change_subscription(
    user_id: str,
    new_plan_id: str,
    seats: int,
    success_redirect: str,
    email: str,
    name: str,
    *,
    consent: "purchase_consent.PurchaseConsentIn",
) -> Dict[str, Any]:
    """Upgrade, downgrade, or reseat a subscription.

    Starts a checkout for the new plan and records which subscription it
    replaces. The current subscription stays ACTIVE until the new one is
    activated by the Mollie webhook; only then is the old one cancelled
    (handle_webhook). An abandoned or failed checkout therefore leaves the
    customer on the old plan — never without a subscription (Fix 18.10.2026:
    previously the old subscription was cancelled first and the checkout then
    failed for lack of `consent`).

    Returns the checkout URL and the ID of the subscription that will be
    replaced once the new one is paid.

    Raises:
      LookupError  — no active subscription found for the user.
      ValueError   — new plan/seats are identical to the current ones (no-op guard).
    """
    # Validate the new plan exists (raises ValueError for unknown).
    _plan_price(new_plan_id)
    from src.budget.plans import get_plan
    app_id = get_plan(new_plan_id).app_id

    # Ersetzt wird das aktive Abo DERSELBEN App (Konto = Nutzer + App), ein
    # abrechnendes Mollie-Abo vor Testphase/Vergabe. Bis 18.10.2026 nahm die
    # Funktion das juengste aktive Abo irgendeiner App.
    pool = get_pool()
    async with pool.acquire() as conn:
        sub = await conn.fetchrow(
            """
            SELECT id, plan_id, seats, mollie_customer_id, mollie_subscription_id
              FROM subscriptions
             WHERE user_id = $1 AND app_id = $2::app_id AND status = 'active'
             ORDER BY (mollie_subscription_id IS NOT NULL) DESC, started_at DESC
             LIMIT 1
            """,
            uuid.UUID(user_id), app_id,
        )

    if not sub:
        raise LookupError(f"No active subscription for user {user_id} in app {app_id}")

    current_plan = sub["plan_id"]
    current_seats = sub["seats"]
    if current_plan == new_plan_id and current_seats == seats:
        raise ValueError(
            f"New plan ({new_plan_id}, {seats} seats) identical to current — nothing to change"
        )

    sub_id = str(sub["id"])
    checkout = await start_subscription_checkout(
        user_id, new_plan_id, seats, success_redirect, email, name,
        consent=consent, ersetzt_abo_id=sub_id,
    )
    return {
        "replacesSubscriptionId": sub_id,
        "cancelledOnActivation": True,
        "previousPlanId": current_plan,
        "newPlanId": new_plan_id,
        "seats": seats,
        "checkoutUrl": checkout.get("checkoutUrl"),
        "paymentId": checkout.get("paymentId"),
    }


async def _pruefe_bezahlten_betrag(order_id: str, payment_id: str, paid_eur: Any) -> None:
    """Bezahlter Mollie-Betrag gegen das Brutto der Bestell-Rechnung.

    Seit 17.10.2026 muessen beide gleich sein (Mollie brutto). Eine Abweichung
    wird LAUT protokolliert (Log-Fehler + billing_event payment.amount_mismatch),
    die bezahlte Bestellung aber trotzdem freigegeben: der Kunde hat gezahlt,
    der Credit steht ihm zu. Typischer Fall: eine vor dem Umstieg gestartete,
    noch netto angelegte Zahlung.
    """
    from decimal import Decimal as _D
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT o.user_id, i.id AS invoice_id, i.total_eur
                 FROM pending_orders o JOIN invoices i ON i.id = o.invoice_id
                WHERE o.id = $1""",
            uuid.UUID(str(order_id)),
        )
    if not row:
        return  # release_order meldet die fehlende Bestellung selbst laut
    if paid_eur is not None and _D(str(paid_eur)).quantize(_CENT) == _D(str(row["total_eur"])).quantize(_CENT):
        return
    logger.error(
        "[webhook] payment %s for order %s: paid %s != invoice total %s",
        payment_id, order_id, paid_eur, row["total_eur"],
    )
    await log_billing_event(
        "payment.amount_mismatch",
        user_id=str(row["user_id"]),
        invoice_id=str(row["invoice_id"]),
        mollie_payment_id=payment_id,
        amount_eur=float(paid_eur) if paid_eur is not None else None,
        source="mollie-webhook",
        payload={"orderId": str(order_id), "invoiceTotalEur": float(row["total_eur"])},
    )


async def handle_webhook(payment_id: str) -> Dict[str, Any]:
    mollie = get_mollie_adapter()
    payment = await mollie.get_payment(payment_id)
    status = payment.get("status")
    if status != "paid":
        # open / pending / authorized are non-terminal — Mollie fires the
        # webhook again once the payment settles, so there is nothing to do.
        # Terminal failures (failed / expired / canceled) get an audit row:
        # activation is gated on status == "paid" so no state is corrupted,
        # but a lost payment must never vanish silently from the trail.
        if status in ("failed", "expired", "canceled"):
            await _record_failed_payment(payment_id, status)
            # Abo-Erstzahlung gescheitert: Abschluss-Platz des Kontos freigeben.
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE abo_abschluesse SET status = 'abgebrochen', geaendert_am = NOW(),
                              letzter_fehler = $2
                        WHERE payment_id = $1 AND status IN ('reserviert', 'offen')""",
                    payment_id, f"Erstzahlung {status}",
                )
            # Recurring subscription payments are NOT in pending_payments.
            # When such a payment fails, suspend the linked subscription.
            mollie_sub_id = payment.get("subscription_id")
            if mollie_sub_id:
                await _suspend_subscription_by_mollie_id(mollie_sub_id, payment_id, status)
            # Self-Service-Pakete (project_pack): terminaler Fehlschlag muss
            # den Order-Lebenszyklus SCHLIESSEN — Order 'expired', Rechnungs-
            # Entwurf 'cancelled' (Rafael, 21.08.). Ohne das blieben Order
            # ewig 'awaiting_payment' und der Entwurf dauerhaft im Operator-
            # Freigabefilter (approval=pending) — versendbar fuer einen Kauf,
            # der nie zustande kam. Fehlende orderId in project_pack-Metadata
            # ist dieselbe Anomalie wie im Paid-Pfad → fail loud (Mollie
            # retried, der Fehler bleibt sichtbar statt still zu versanden).
            if payment.get("metadata", {}).get("type") == "project_pack":
                order_id = payment.get("metadata", {}).get("orderId")
                if not order_id:
                    raise RuntimeError(
                        f"project_pack webhook ({status}): orderId missing "
                        f"from payment {payment_id} metadata"
                    )
                from src.billing.pending_orders_service import expire_order_for_failed_payment
                expired = await expire_order_for_failed_payment(order_id, payment_id, status)
                return {
                    "handled": True,
                    "type": "project_pack",
                    "order": expired,
                    "reason": f"status={status}",
                }
        return {"handled": False, "reason": f"status={status}"}

    pool = get_pool()
    async with pool.acquire() as conn:
        pending = await conn.fetchrow(
            "SELECT user_id, type, plan_id, amount_eur, ersetzt_abo_id "
            "FROM pending_payments WHERE payment_id = $1",
            payment_id,
        )
    if not pending:
        return {"handled": False, "reason": "unknown payment_id"}

    user_id = str(pending["user_id"])
    if pending["type"] == "topup":
        result = await _credit_topup(user_id, float(pending["amount_eur"]), payment_id)
        return {"handled": True, "type": "topup", "result": result}

    if pending["type"] == "subscription_first":
        seats = int(payment.get("metadata", {}).get("seats", 1))
        cust = payment.get("customer_id")
        if not cust:
            # FakeMollie: customer_id steckt im stored payment
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT mollie_customer_id FROM mollie_customers WHERE user_id = $1",
                    uuid.UUID(user_id),
                )
            cust = row["mollie_customer_id"] if row else None
        if not cust:
            raise RuntimeError("subscription_first: customer_id missing")
        sub = await _activate_subscription(
            user_id, pending["plan_id"], seats, cust, first_payment_id=payment_id,
            ersetzt_abo_id=pending["ersetzt_abo_id"],
        )
        # Planwechsel: das alte Abo erst JETZT kuendigen, wo das neue aktiv ist —
        # und nur, solange das neue aktiv ist. Scheitert die Kuendigung, steht
        # der Wechsel auf 'doppelt_aktiv' (Alarm) und der Webhook antwortet 500;
        # Mollie und der Nachholauftrag wiederholen.
        wechsel = await _wechsel_abschliessen(payment_id)
        ersetzt = wechsel.get("altAboId") if wechsel.get("ergebnis") == "alt_gekuendigt" else None
        if ersetzt:
            logger.info(
                "[webhook] plan change: subscription %s replaced by %s (payment %s)",
                ersetzt, sub.get("id"), payment_id,
            )
        return {"handled": True, "type": "subscription_first", "subscription": sub,
                "replacedSubscriptionId": ersetzt,
                "wechsel": {"status": wechsel.get("status"), "ergebnis": wechsel.get("ergebnis")}}

    if pending["type"] == "project_pack":
        # Self-Service-Projekt-Paket: dieselbe Freigabe-Logik wie der Operator,
        # nur automatisch ausgelöst durch die bezahlte Mollie-Zahlung.
        order_id = payment.get("metadata", {}).get("orderId")
        if not order_id:
            raise RuntimeError("project_pack webhook: orderId missing from payment metadata")
        from src.billing.pending_orders_service import release_order, OrderNotReleasableError
        await _pruefe_bezahlten_betrag(order_id, payment_id, payment.get("amount_eur"))
        try:
            released = await release_order(
                order_id, operator_user_id=None, note="self-service Mollie payment",
            )
            # Rechnung mit der Mollie-Zahlung verknuepfen. release_order legt sie
            # an, wenn die Bestellung freigegeben wird — zu diesem Zeitpunkt gibt
            # es noch keine Zahlungs-ID. Bekannt ist sie erst hier, und ohne
            # diesen Nachtrag bleibt mollie_payment_id NULL: der Abgleich
            # Zahlung<->Rechnung ist dann Handarbeit (beobachtet 06.08.: KEINE
            # Rechnung der letzten Wochen trug eine Zahlungs-ID).
            #
            # COALESCE statt blindem Setzen: die Spalte ist UNIQUE, und ein
            # Webhook-Retry darf eine bereits verknuepfte Rechnung nicht mit
            # einer zweiten ID ueberschreiben. Fehler hier duerfen die schon
            # freigegebene, bezahlte Bestellung nicht zurueckdrehen — Mollie
            # wuerde sonst release_order erneut anstossen.
            try:
                pool = get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE invoices SET mollie_payment_id = COALESCE(mollie_payment_id, $1), "
                        "updated_at = NOW() WHERE id = $2",
                        payment_id,
                        uuid.UUID(released["invoiceId"]),
                    )
            except Exception as exc:  # noqa: BLE001 — Verknuepfung ist Buchhaltungs-Komfort, keine Kaufbedingung
                logger.warning(
                    "[webhook] could not link invoice %s to payment %s: %s",
                    released.get("invoiceId"), payment_id, exc,
                )
            if released["planId"] in AUTO_SEND_INVOICE_PLAN_IDS:
                # Best-effort, mirrors create_pending_order's email-send guard:
                # the order is already released and paid — a Resend hiccup must
                # not turn an already-committed, successful purchase into a
                # webhook failure (which would make Mollie retry release_order).
                from src.invoices.routes import auto_approve_and_send_invoice
                try:
                    await auto_approve_and_send_invoice(
                        released["invoiceId"], actor="system:mollie-auto-send",
                    )
                except Exception as exc:  # noqa: BLE001 — silent-ok: invoice is issued+paid regardless; operator can send manually via Platform Admin if this fails
                    import logging
                    logging.getLogger(__name__).warning(
                        "Auto-send invoice %s for order %s failed (order released, "
                        "invoice paid — manual send via Platform Admin still possible): %s",
                        released["invoiceId"], order_id, exc,
                    )
            return {"handled": True, "type": "project_pack", "order": released}
        except OrderNotReleasableError as exc:
            if exc.status == "released":
                # Webhook-Retry nach bereits erfolgter Freigabe → idempotent ok.
                return {"handled": True, "type": "project_pack", "idempotent": True}
            # Bezahlte Mollie-Zahlung für eine 'expired'/'cancelled' Order ist
            # eine echte Anomalie (Geld erhalten, aber Order nicht freigebbar)
            # → fail loud, kein stilles Schlucken.
            raise RuntimeError(
                f"project_pack: paid Mollie payment {payment_id} for order {order_id} "
                f"in non-releasable state '{exc.status}' — manual investigation required"
            )

    return {"handled": False, "reason": f"unknown type {pending['type']}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_subscription(row: Any) -> Dict[str, Any]:
    def _ts(col: str) -> Optional[str]:
        v = row[col] if col in row.keys() else None
        return v.isoformat() if v else None

    return {
        "id": str(row["id"]),
        "userId": str(row["user_id"]),
        "appId": row["app_id"],
        "planId": row["plan_id"],
        "status": row["status"],
        "mollieCustomerId": row["mollie_customer_id"],
        "mollieSubscriptionId": row["mollie_subscription_id"],
        "seats": row["seats"],
        "startedAt": _ts("started_at"),
        "cancelledAt": _ts("cancelled_at"),
        "suspendedAt": _ts("suspended_at"),
        "expiredAt": _ts("expired_at"),
        "trialEndsAt": _ts("trial_ends_at"),
    }


# ---------------------------------------------------------------------------
# Billing-event audit trail
# ---------------------------------------------------------------------------

async def log_billing_event(
    event_type: str,
    *,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
    invoice_id: Optional[str] = None,
    mollie_payment_id: Optional[str] = None,
    amount_eur: Optional[float] = None,
    source: str = "system",
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Append a row to billing_events. Append-only — never updates.

    tenant_id falls back to the user's tenant when not given explicitly, so
    billing events stay mode-filterable even if a caller forgets it. A truly
    user-less system event (no user_id, no tenant_id) is allowed — those are
    global and not tenant-scoped. See ADR 0007.
    """
    import json as _json
    if tenant_id is None and user_id:
        from src.api_auth.tenant_resolver import resolve_tenant_for_user
        tenant_id = await resolve_tenant_for_user(user_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO billing_events
              (event_type, user_id, tenant_id, subscription_id, invoice_id,
               mollie_payment_id, amount_eur, source, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            """,
            event_type,
            uuid.UUID(user_id) if user_id else None,
            tenant_id,
            uuid.UUID(subscription_id) if subscription_id else None,
            uuid.UUID(invoice_id) if invoice_id else None,
            mollie_payment_id,
            amount_eur,
            source,
            _json.dumps(payload or {}),
        )


# ---------------------------------------------------------------------------
# Direct subscription provisioning — for seed/test environments (no Mollie).
# ---------------------------------------------------------------------------

async def _provision_plan_budget(
    conn: Any, user_id: uuid.UUID, plan: Any
) -> None:
    """Insert a monthly budget entry for a paid plan — idempotent.

    Same guard as _provision_trial: the ON CONFLICT UPDATE fires only when the
    plan key is absent, so existing usage is never reset by a re-seed.
    Reset window: 30 days (standard monthly billing cycle).
    """
    valid_until = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    entry_json = json.dumps({
        plan.id: {
            "limitEur": float(plan.api_budget_eur),
            "usedEur": 0.0,
            "resetAt": valid_until,
        }
    })
    await conn.execute(
        """
        INSERT INTO user_budgets (user_id, monthly_budgets, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET monthly_budgets = user_budgets.monthly_budgets || $2::jsonb,
            updated_at = NOW()
        WHERE (user_budgets.monthly_budgets -> $3) IS NULL
        """,
        user_id,
        entry_json,
        plan.id,
    )


# Fields required on tenants.billing_* for a legally issuable invoice. vat_id
# stays optional — B2C customers (no UID) are explicitly allowed by §11 UStG.
# Keep this list in sync with migration 010_tenant_billing_address.sql comment.
_REQUIRED_BILLING_ADDRESS_FIELDS = ("billing_name", "billing_street", "billing_city",
                                     "billing_postcode", "billing_country")


# Exempt from the billing-address gate. Per the required-fields-yaml SSoT
# (packages/api-validation/required-fields.yaml stage_exemptions):
#   - 'test'     — seeded test tenants, fill defaults via seed-bridge-users
#                  Phase 2b but a re-seed against a freshly-migrated DB still
#                  needs to provision subscriptions BEFORE Phase 2b runs.
#   - 'internal' — internal tenants (employees, partners) bypass the customer
#                  lifecycle entirely.
# Only account_type='customer' goes through the gate. See the research
# baseline in docs/research/registration-required-fields-AT-EU-20260523.md
# for the legal reasoning (§11 UStG applies only to invoiceable B2B sales).
_BILLING_ADDRESS_GATE_EXEMPT_ACCOUNT_TYPES = frozenset({"test", "internal"})


async def _assert_complete_billing_address(
    conn: Any, user_uuid: uuid.UUID, plan_id: str
) -> None:
    """
    Refuse to proceed if a CUSTOMER tenant lacks any field that an Austrian
    invoice legally requires (§11 UStG: name + full address). The check runs
    at the subscription-provision boundary so an "active subscription without
    invoice-able state" never exists — defensive, fail-fast.

    Two-tier exemption (per packages/api-validation/required-fields.yaml):
      1. Trial plans skip the gate — trials produce no invoice, so §11
         doesn't bite. The address will be required at upgrade-to-paid time
         via the Mollie checkout flow.
      2. Tenants with account_type in {test, internal} skip the gate —
         test seeders + internal accounts bypass the customer lifecycle.

    For account_type='customer' on a non-trial plan: raises ValueError
    (-> 400 at the route layer) listing the missing fields so the caller
    knows exactly what to backfill via PATCH
    /v1/tenants/{tid}/billing-address.
    """
    # Trial plans are exempt — checked first to avoid a DB round-trip.
    from src.budget.plans import get_plan
    if get_plan(plan_id).trial:
        return

    row = await conn.fetchrow(
        """
        SELECT t.id           AS tenant_id,
               t.account_type::text AS account_type,
               t.billing_name, t.billing_street, t.billing_city,
               t.billing_postcode, t.billing_country
        FROM users u
        JOIN tenants t ON t.id = u.tenant_id
        WHERE u.id = $1
        """,
        user_uuid,
    )
    if row is None:
        raise ValueError(
            f"provision_subscription: user '{user_uuid}' has no tenant — "
            "cannot determine billing address"
        )

    # Test + internal tenants are exempt — see exemption-set comment above.
    if row["account_type"] in _BILLING_ADDRESS_GATE_EXEMPT_ACCOUNT_TYPES:
        return

    missing = [f for f in _REQUIRED_BILLING_ADDRESS_FIELDS
               if not (row[f] or "").strip()]
    if missing:
        # Strip the `billing_` prefix in the error message to match the API
        # field names the caller would use to PATCH /v1/tenants/{tid}/billing-address.
        api_names = [f.removeprefix("billing_") for f in missing]
        raise ValueError(
            f"provision_subscription: customer tenant '{row['tenant_id']}' "
            f"is missing required billing-address fields: {api_names}. "
            f"PATCH /v1/tenants/{{tenant_id}}/billing-address before provisioning."
        )


async def _ensure_app_license(
    conn, user_uuid: uuid.UUID, app_id: str, plan_id: str, seats: int
) -> None:
    """Idempotent UPSERT of the app_license for (user, app).

    The login JWT reads its `appLicenses` claim from the app_licenses table —
    NOT from subscriptions. An active subscription without the matching license
    is therefore invisible to the apps (403). Granting the license in lockstep
    with the subscription (see provision_subscription) means the "subscription
    provisioned but license missing" intermediate state — seed Phase 5 run
    without Phase 6, which made interactive@'s badge flicker between provisioning
    runs — can no longer exist.

    Paid provisioning → start today, no end_date (perpetual until cancelled).
    UPSERT mirrors admin grant_app_license so there is one license-grant shape.
    """
    await conn.execute(
        """
        INSERT INTO app_licenses (user_id, app_id, plan_id, start_date, end_date, seats)
        VALUES ($1, $2::app_id, $3::plan_id, CURRENT_DATE, NULL, $4)
        ON CONFLICT (user_id, app_id) DO UPDATE
          SET plan_id    = EXCLUDED.plan_id,
              start_date = EXCLUDED.start_date,
              end_date   = EXCLUDED.end_date,
              seats      = EXCLUDED.seats
        """,
        user_uuid, app_id, plan_id, seats,
    )


async def provision_subscription(
    user_id: str,
    plan_id: str,
    seats: int,
) -> Dict[str, Any]:
    """
    Directly provision an active subscription without Mollie — for seeding.

    Produces exactly the state that a successful Mollie checkout + webhook cycle
    would produce (status='active', monthly budget entry set) without initiating
    a payment. Synthetic placeholder values fill the Mollie-specific columns so
    callers without a real Mollie customer can still own a valid subscription row.

    Idempotent: if the user already has an active subscription for this plan,
    returns it without creating a duplicate. Refuses trial plans (use the normal
    checkout flow for those — trial provisioning happens auto in evaluate_budget).

    Fail-fast: refuses to provision if the user's tenant has no complete
    billing-address (§11 UStG — invoices require a recipient address). Seeders
    must populate the address before provisioning; FE checkout flows must
    require it during onboarding.

    The synthetic mollie_first_payment_id ('provision-{user_id}-{plan_id}') acts
    as the idempotency key for concurrent callers, just like the real payment ID
    does in _activate_subscription.
    """
    from src.budget.plans import get_plan

    plan = get_plan(plan_id)  # raises ValueError for unknown plan
    if plan.trial:
        raise ValueError(
            f"provision_subscription: cannot provision trial plan '{plan_id}' — "
            "trials are auto-provisioned via evaluate_budget"
        )

    pool = get_pool()
    user_uuid = uuid.UUID(user_id)

    # Fail-fast pre-check: a customer subscription without billing-address
    # yields invoices that cannot legally be issued (§11 UStG). Refuse here
    # so the incomplete state never exists, rather than discovering it
    # later when auto_create_invoice silently marks the invoice 'incomplete'.
    # Trial plans + test/internal tenants are exempt (see helper docstring).
    async with pool.acquire() as conn:
        await _assert_complete_billing_address(conn, user_uuid, plan_id)

    # Pre-check: if the user already owns an active subscription for this plan,
    # return it immediately. This covers the common idempotent re-seed case
    # without going through the INSERT path.
    async with pool.acquire() as conn:
        # Grant the app_license FIRST — idempotent and independent of subscription
        # state, so it also repairs an existing-subscription-but-missing-license
        # user (not just freshly-provisioned ones). License + subscription + budget
        # now always move together; "active sub but 403" can no longer occur.
        await _ensure_app_license(conn, user_uuid, plan.app_id, plan_id, seats)

        existing = await conn.fetchrow(
            """
            SELECT id, user_id, app_id, plan_id, status, mollie_customer_id,
                   mollie_subscription_id, seats, started_at, cancelled_at,
                   suspended_at, expired_at
            FROM subscriptions
            WHERE user_id = $1 AND plan_id = $2::plan_id AND status = 'active'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            user_uuid, plan_id,
        )
        if existing:
            # Self-heal: an active sub provisioned before the pot became
            # mandatory on this path (or via a caller that skipped it) never
            # got one. _ensure_monthly_budget_pot only inserts when the lane
            # is absent, so an in-use pot is never touched.
            await _ensure_monthly_budget_pot(conn, user_uuid, plan_id)
            return _serialize_subscription(existing)

        # No active subscription yet. Insert with synthetic Mollie identifiers.
        # mollie_first_payment_id is the UNIQUE idempotency key — concurrent callers
        # that both passed the pre-check will race here; one wins, one hits ON CONFLICT.
        synth_payment_id = f"provision-{user_id}-{plan_id}"
        synth_customer_id = f"seed-{user_id}"
        sub_uuid = uuid.uuid4()

        row = await conn.fetchrow(
            """
            INSERT INTO subscriptions
              (id, user_id, app_id, plan_id, status,
               mollie_customer_id, mollie_subscription_id, mollie_first_payment_id,
               seats, started_at, metadata)
            VALUES ($1, $2, $3, $4::plan_id, 'active',
                    $5, NULL, $6, $7, NOW(), '{"provisioned": true}'::jsonb)
            ON CONFLICT (mollie_first_payment_id) DO NOTHING
            RETURNING id, user_id, app_id, plan_id, status, mollie_customer_id,
                      mollie_subscription_id, seats, started_at, cancelled_at,
                      suspended_at, expired_at
            """,
            sub_uuid, user_uuid, plan.app_id, plan_id,
            synth_customer_id, synth_payment_id, seats,
        )

        if row:
            await _ensure_monthly_budget_pot(conn, user_uuid, plan_id)
            sub_dict = _serialize_subscription(row)
            sub_id = sub_dict["id"]
        else:
            # ON CONFLICT: concurrent caller won the race. Return their row.
            winner = await conn.fetchrow(
                """
                SELECT id, user_id, app_id, plan_id, status, mollie_customer_id,
                       mollie_subscription_id, seats, started_at, cancelled_at,
                       suspended_at, expired_at
                FROM subscriptions WHERE mollie_first_payment_id = $1
                """,
                synth_payment_id,
            )
            if not winner:
                raise RuntimeError(
                    f"provision_subscription: ON CONFLICT fired but no row found "
                    f"for payment_id={synth_payment_id}"
                )
            sub_dict = _serialize_subscription(winner)
            sub_id = sub_dict["id"]

    await log_billing_event(
        "subscription.provisioned",
        user_id=user_id,
        subscription_id=sub_id,
        source="seed",
        payload={"planId": plan_id, "seats": seats},
    )
    return sub_dict


# ---------------------------------------------------------------------------
# Auto-invoice helper — called from Mollie-webhook handlers after a paid event.
# ---------------------------------------------------------------------------

async def auto_create_invoice(
    *,
    user_id: str,
    amount_eur: float,
    description: str,
    subscription_id: str | None = None,
    credit_purchase_id: str | None = None,
    mollie_payment_id: str | None = None,
    number_prefix: str = FALLBACK_PREFIX,
) -> str | None:
    """Insert one invoice row, mark it 'paid', return the id.

    Idempotent: if an invoice already exists for this mollie_payment_id, return
    the existing id without re-inserting (fixes Mollie webhook retries).

    Resolves the tenant's billing address to populate invoices.billing_address
    and determine the correct tax rate (AT: 20%, EU B2B Reverse Charge: 0%,
    non-EU export: 0%).  If the address is missing the invoice is still created
    — the payment happened and the record MUST exist — but metadata carries
    {"incomplete": True, "missingBillingAddress": True} so operators can
    identify and correct the gap.

    EU B2C tax (OSS-Verfahren) is an OPEN DECISION: see _determine_tax_rate.
    """
    import json as _json
    from decimal import Decimal
    from datetime import datetime, timezone

    pool = get_pool()
    if mollie_payment_id:
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT id FROM invoices WHERE mollie_payment_id = $1",
                mollie_payment_id,
            )
        if existing:
            return str(existing)

    tax_info = await resolve_invoice_tax(user_id, log_prefix="[invoice]")
    tenant_id = tax_info["tenantId"]
    billing_address = tax_info["billingAddress"]
    tax_rate = tax_info["taxRate"]
    reverse_charge_note = tax_info["reverseChargeNote"]
    eu_b2c_flag = tax_info["euB2c"]

    # Abos und Top-Ups: amount_eur ist der NETTO-Betrag, die Rechnung schlaegt
    # die USt auf. Mollie hat bei diesen Bahnen bisher den Nettobetrag
    # eingezogen (offener Befund, s. ERGEBNIS 17.10.2026) — hier unveraendert.
    subtotal, tax, total = invoice_amounts(round(float(amount_eur), 2), tax_rate)

    line_items = [{
        "description": description,
        "quantity": 1,
        "unitPriceEur": float(subtotal),
        "totalEur": float(subtotal),
        "metadata": {},
    }]

    metadata: Dict[str, Any] = {"autoCreated": True, "source": "mollie-webhook"}
    if billing_address is None:
        metadata["incomplete"] = True
        metadata["missingBillingAddress"] = True
    if reverse_charge_note:
        metadata["reverseChargeNote"] = reverse_charge_note
    if eu_b2c_flag:
        # EU B2C without vatId — OSS-Verfahren applies, tax rate is approximate.
        # OPEN DECISION: must be reviewed with tax advisor before serving EU B2C volume.
        metadata["taxNote"] = "EU_B2C_OSS_OPEN"

    async with pool.acquire() as conn:
        async with conn.transaction():
            invoice_number = await next_invoice_number(conn, number_prefix)

            row = await conn.fetchrow(
                """
                INSERT INTO invoices
                  (invoice_number, user_id, tenant_id, subscription_id, credit_purchase_id,
                   mollie_payment_id, status, subtotal_eur, tax_rate, tax_eur, total_eur,
                   currency, line_items, billing_address, issued_at, paid_at, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, 'paid', $7, $8, $9, $10, 'EUR', $11::jsonb,
                        $12::jsonb, NOW(), NOW(), $13::jsonb)
                ON CONFLICT (mollie_payment_id) DO NOTHING
                RETURNING id
                """,
                invoice_number,
                uuid.UUID(user_id),
                tenant_id,
                uuid.UUID(subscription_id) if subscription_id else None,
                uuid.UUID(credit_purchase_id) if credit_purchase_id else None,
                mollie_payment_id,
                subtotal, tax_rate, tax, total,
                _json.dumps(line_items),
                _json.dumps(billing_address) if billing_address else None,
                _json.dumps(metadata),
            )

    if row:
        invoice_id = str(row["id"])
        await log_billing_event(
            "invoice.issued",
            user_id=user_id,
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            invoice_id=invoice_id,
            mollie_payment_id=mollie_payment_id,
            amount_eur=float(total),
            source="mollie-webhook",
            payload={"invoiceNumber": invoice_number, "auto": True},
        )
        return invoice_id
    # ON CONFLICT means another concurrent call won — find their id.
    if mollie_payment_id:
        async with pool.acquire() as conn:
            return str(await conn.fetchval(
                "SELECT id FROM invoices WHERE mollie_payment_id = $1",
                mollie_payment_id,
            ))
    return None


async def seed_legacy_trials(app_id: str) -> Dict[str, int]:
    """
    Backfill: for every user without an active subscription for `app_id`,
    insert a 7-day trial subscription + app_license. Idempotent.

    Mirrors the trial-seeding path in identity/routes.py register() so legacy
    users (created before the trial-auto-seeding landed) get the same grace
    window. Returns counts so callers can verify scope before tightening the
    budget gate to block `unlicensed`.

    trial_ends_at = NOW + 7 days. start_date = today. Same rules as register.
    """
    pool = get_pool()
    now = datetime.now(timezone.utc)
    today = now.date()
    trial_ends = now + timedelta(days=7)

    created = 0
    skipped = 0

    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT id FROM users")
        for u in users:
            uid = u["id"]
            existing = await conn.fetchval(
                """
                SELECT 1 FROM subscriptions
                WHERE user_id = $1 AND app_id = $2::app_id AND status = 'active'
                LIMIT 1
                """,
                uid, app_id,
            )
            if existing:
                skipped += 1
                continue

            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO app_licenses (user_id, app_id, plan_id, start_date, end_date, seats)
                    VALUES ($1, $2::app_id, 'trial'::plan_id, $3, NULL, 1)
                    ON CONFLICT DO NOTHING
                    """,
                    uid, app_id, today,
                )
                await conn.execute(
                    """
                    INSERT INTO subscriptions
                        (user_id, app_id, plan_id, status, mollie_customer_id,
                         seats, started_at, trial_ends_at)
                    VALUES
                        ($1, $2::app_id, 'trial'::plan_id, 'active'::subscription_status,
                         NULL, 1, $3, $4)
                    """,
                    uid, app_id, now, trial_ends,
                )
            created += 1

    return {"app_id": app_id, "created": created, "skipped": skipped, "total": len(users)}

