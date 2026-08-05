"""
purchase_consent — die Kaufzustimmung als Beweisstück.

Bis 2026-08-05 lebte die Zustimmung nur im Browser: das Frontend baute sie,
prüfte sie selbst und warf sie dann weg. Dieses Modul nimmt sie entgegen,
prüft sie ein zweites Mal serverseitig und schreibt sie nach
`purchase_consents` (Migration 054).

ZWEI REGELN, DIE HIER NICHT VERHANDELBAR SIND:

1. Kein Kauf ohne Zustimmung. `consent` ist an den Selbstbedienungs-Checkouts
   ein PFLICHTFELD, kein Optional mit Default. Ein fehlendes Feld ist ein
   Fehler des Aufrufers und wird laut abgewiesen — nicht stillschweigend als
   "hat halt nicht zugestimmt" durchgewunken.

2. Der Beleg entsteht VOR der Zahlung. `record_consent` läuft, bevor die
   Mollie-Zahlung angelegt wird. Schlägt das Schreiben fehl, entsteht keine
   Zahlung. Umgekehrt wäre schlimmer: Geld eingenommen, Beweis verloren.

Was NICHT hier passiert: eine Client-Behauptung wird nicht zur Server-Wahrheit
umdeklariert. `accepted_at` bleibt die Angabe des Clients, `recorded_at` setzt
die Datenbank. Wer beides vergleicht, sieht Abweichungen — das ist Absicht.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

# Die vier Bahnen, die Selbstbedienungskäufe auslösen. Muss zum CHECK
# purchase_consents_lane_known in Migration 054 passen.
LANE_SUBSCRIPTION = "subscription"
LANE_PROJECT_PACK = "project_pack"
LANE_FIRST_PURCHASE_PACK = "first_purchase_pack"
LANE_TOPUP = "topup"

_KNOWN_LANES = frozenset(
    {LANE_SUBSCRIPTION, LANE_PROJECT_PACK, LANE_FIRST_PURCHASE_PACK, LANE_TOPUP}
)


class PurchaseConsentIn(BaseModel):
    """
    Die Zustimmung, wie das Frontend sie baut (portal/legal/PurchaseConsent.tsx).

    Feldnamen sind bewusst camelCase — sie kommen 1:1 aus dem TypeScript-Objekt,
    und ein Umbenennen auf dem Weg wäre eine stille Stelle, an der die beiden
    Seiten auseinanderlaufen können.
    """

    termsVersion: str = Field(min_length=1, max_length=32)
    actingAsBusiness: bool
    professionallyQualified: bool
    acceptedAt: datetime

    @field_validator("actingAsBusiness", "professionallyQualified")
    @classmethod
    def _must_be_declared(cls, v: bool) -> bool:
        # Eine nicht erteilte Erklärung ist keine Zustimmung. Sie hier
        # anzunehmen und später "irgendwie" zu behandeln, wäre genau die
        # Grauzone, die dieses Modul beseitigen soll.
        if v is not True:
            raise ValueError(
                "purchase consent incomplete: both declarations (actingAsBusiness, "
                "professionallyQualified) must be explicitly true — an unchecked "
                "declaration is not a consent"
            )
        return v


async def record_consent(
    conn: Any,
    *,
    consent: PurchaseConsentIn,
    user_id: str,
    lane: str,
    order_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    quantity: Optional[int] = None,
    amount_eur: Optional[float] = None,
) -> str:
    """
    Schreibt die erteilte Zustimmung und gibt ihre id zurück.

    Der Mandant wird aus dem Nutzer aufgelöst statt vom Aufrufer übernommen:
    ein Beleg, dessen Zuordnung der Aufrufer bestimmen darf, ist fälschbar.

    Raises:
        ValueError — unbekannte Bahn, oder der Nutzer hat keinen Mandanten.
                     Beides ist ein Programmierfehler, kein Kundenfehler.
    """
    if lane not in _KNOWN_LANES:
        raise ValueError(
            f"unknown checkout lane '{lane}' — allowed: {sorted(_KNOWN_LANES)}"
        )

    user_uuid = uuid.UUID(user_id)
    tenant_row = await conn.fetchrow(
        "SELECT tenant_id FROM users WHERE id = $1", user_uuid
    )
    if tenant_row is None or not tenant_row["tenant_id"]:
        raise ValueError(
            f"record_consent: user '{user_id}' has no tenant — cannot file a "
            "consent record without the party it belongs to"
        )

    row = await conn.fetchrow(
        """
        INSERT INTO purchase_consents (
            user_id, tenant_id, lane, order_id,
            plan_id, quantity, amount_eur,
            terms_version, acting_as_business, professionally_qualified,
            accepted_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        user_uuid,
        tenant_row["tenant_id"],
        lane,
        uuid.UUID(order_id) if order_id else None,
        plan_id,
        quantity,
        amount_eur,
        consent.termsVersion,
        consent.actingAsBusiness,
        consent.professionallyQualified,
        consent.acceptedAt,
    )
    return str(row["id"])
