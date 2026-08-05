"""
vat_id — UID-Nummern gegen VIES pruefen, bevor sie 0 % Umsatzsteuer ausloesen.

DAS PROBLEM, DAS DIESES MODUL LOEST
`_determine_tax_rate` behandelte jeden nicht-leeren `vatId` als B2B-Signal:
EU-Ausland + irgendein Text im Feld ergab 0 % USt mit Reverse-Charge-Vermerk.
Ein Kunde, der sich vertippt, bekam damit eine Nullsteuer-Rechnung. § 19 Abs 1
UStG verlagert die Steuerschuld aber nur bei einer GUELTIGEN UID des
Leistungsempfaengers — ist sie ungueltig, schuldet der Aussteller die Steuer.
Das faellt erst bei einer Betriebspruefung auf, dann rueckwirkend.

DIE BEIDEN REGELN
1. NUR eine bestaetigte Pruefung erlaubt Reverse Charge. Kein Ergebnis,
   Zeitueberschreitung, VIES-Ausfall, Mitgliedsland nicht erreichbar → KEIN
   Reverse Charge, also der sichere Default (20 % AT USt). Zu viel Steuer ist
   korrigierbar, zu wenig kostet. Es gibt hier bewusst KEINEN Pfad, auf dem
   ein Fehler als "gueltig" durchrutscht.
2. Jedes Ergebnis wird gespeichert (Zeitpunkt + vollstaendige Antwort). Das
   ist die Aufzeichnung nach § 18 UStG; eine Pruefung, die niemand belegen
   kann, ist im Ernstfall keine.

WARUM DIE PRUEFUNG BEIM SPEICHERN DER ADRESSE LAEUFT UND NICHT BEI DER RECHNUNG
VIES ist ein fremder Dienst mit realen Ausfaellen. Haenge die Rechnungs-
erstellung daran, steht im Ausfall der Verkauf — oder jemand baut einen
Fallback ein, und genau der wird dann zur stillen Nullsteuer-Quelle. Deshalb:
beim Erfassen pruefen, Ergebnis hinterlegen, im Steuerpfad nur nachschlagen.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# Offizieller REST-Endpunkt der EU-Kommission. Kein Schluessel noetig.
_VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{cc}/vat/{num}"

# Bewusst knapp: der Aufruf haengt an einer Nutzerinteraktion (Adresse
# speichern). Laeuft er in die Zeitueberschreitung, wird NICHT bestaetigt —
# der Kunde kann erneut speichern, die Rechnung bleibt bis dahin mit USt.
_TIMEOUT_S = 10.0

# Wie lange eine Bestaetigung als aktuell gilt. UIDs werden entzogen; eine
# Bestaetigung von vor einem Jahr belegt den heutigen Status nicht.
VALIDATION_MAX_AGE_DAYS = 180

_CLEAN = re.compile(r"[\s.\-/]")


class VatIdCheckUnavailable(RuntimeError):
    """VIES war nicht erreichbar oder hat keine verwertbare Antwort geliefert.

    Bewusst eine eigene Klasse: der Aufrufer muss den Unterschied zwischen
    "ungueltig" (Kunde korrigieren lassen) und "nicht pruefbar" (nicht die
    Schuld des Kunden, aber auch kein Reverse Charge) behandeln koennen.
    """


def normalize(vat_id: str) -> str:
    """Leerzeichen/Punkte/Bindestriche weg, Grossbuchstaben.

    "ATU 781 566-38" und "atu78156638" sind dieselbe Nummer; ohne
    Normalisierung waeren es zwei verschiedene Nachschlage-Schluessel und die
    gespeicherte Bestaetigung wuerde nicht gefunden.
    """
    return _CLEAN.sub("", (vat_id or "")).upper()


def split_country(vat_id: str) -> Tuple[str, str]:
    """Zerlegt eine normalisierte UID in Laenderkuerzel und Nummer.

    Wirft bei offensichtlichem Unsinn — eine UID ohne fuehrendes Laenderkuerzel
    ist keine, und sie ungeprueft an VIES zu schicken wuerde nur eine
    nichtssagende Fehlermeldung erzeugen.
    """
    norm = normalize(vat_id)
    if len(norm) < 4 or not norm[:2].isalpha():
        raise ValueError(
            f"vat id {vat_id!r} has no leading ISO country code — expected e.g. 'ATU12345678'"
        )
    return norm[:2], norm[2:]


async def check_vies(vat_id: str) -> Dict[str, Any]:
    """Fragt VIES. Gibt die Antwort zurueck oder wirft VatIdCheckUnavailable.

    Ein `isValid: false` ist KEIN Fehler — es ist ein gueltiges Ergebnis und
    wird zurueckgegeben. Geworfen wird nur, wenn keine Aussage moeglich war.
    """
    cc, num = split_country(vat_id)
    url = _VIES_URL.format(cc=cc, num=num)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
    except Exception as e:  # Netzwerk, DNS, Timeout
        raise VatIdCheckUnavailable(f"VIES unreachable for {cc}: {e}") from e

    if resp.status_code != 200:
        raise VatIdCheckUnavailable(
            f"VIES returned HTTP {resp.status_code} for {cc} — no statement possible"
        )
    try:
        data = resp.json()
    except Exception as e:
        raise VatIdCheckUnavailable(f"VIES sent no parseable JSON: {e}") from e

    if not isinstance(data, dict) or "isValid" not in data:
        raise VatIdCheckUnavailable(f"VIES response lacks isValid: {str(data)[:160]}")

    # Manche Mitgliedslaender melden sich zeitweise ab; VIES antwortet dann mit
    # 200 und einem userError. Das ist "nicht pruefbar", nicht "ungueltig" —
    # und darf deshalb NICHT als negatives Ergebnis gespeichert werden.
    user_error = (data.get("userError") or "").upper()
    if not data["isValid"] and user_error not in ("INVALID", "VALID", ""):
        raise VatIdCheckUnavailable(
            f"VIES could not answer for {cc}: userError={user_error}"
        )
    return data


async def validate_and_store(conn: Any, *, tenant_id: str, vat_id: str) -> Dict[str, Any]:
    """Prueft die UID und schreibt das Ergebnis nach vat_id_validations.

    Rueckgabe: {'isValid': bool, 'name': str|None, 'address': str|None}.
    Wirft VatIdCheckUnavailable, wenn VIES keine Aussage geliefert hat — dann
    wird NICHTS gespeichert, und der Steuerpfad findet keine Bestaetigung.
    """
    norm = normalize(vat_id)
    cc, _ = split_country(norm)
    data = await check_vies(norm)

    await conn.execute(
        """
        INSERT INTO vat_id_validations
            (id, tenant_id, vat_id, country_code, is_valid, vies_name, vies_address, response_raw)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        """,
        str(uuid.uuid4()),
        tenant_id,
        norm,
        cc,
        bool(data.get("isValid")),
        _clean_vies_field(data.get("name")),
        _clean_vies_field(data.get("address")),
        json.dumps(data),
    )
    logger.info(
        "[vat_id] checked tenant=%s vat=%s valid=%s", tenant_id, norm, data.get("isValid")
    )
    return {
        "isValid": bool(data.get("isValid")),
        "name": _clean_vies_field(data.get("name")),
        "address": _clean_vies_field(data.get("address")),
    }


async def has_confirmed_validation(conn: Any, *, tenant_id: str, vat_id: str) -> bool:
    """Gibt es eine BESTAETIGTE, nicht zu alte Pruefung fuer genau diese UID?

    Das ist die einzige Frage, die der Steuerpfad stellen darf. Kein Eintrag,
    ein negativer Eintrag oder ein zu alter Eintrag heissen alle dasselbe:
    kein Reverse Charge.
    """
    norm = normalize(vat_id)
    if not norm:
        return False
    row = await conn.fetchrow(
        """
        SELECT is_valid
          FROM vat_id_validations
         WHERE tenant_id = $1
           AND vat_id = $2
           AND checked_at > now() - ($3 || ' days')::interval
         ORDER BY checked_at DESC
         LIMIT 1
        """,
        tenant_id,
        norm,
        str(VALIDATION_MAX_AGE_DAYS),
    )
    return bool(row and row["is_valid"])


def _clean_vies_field(v: Optional[str]) -> Optional[str]:
    """VIES liefert '---', wenn ein Mitgliedsland den Namen nicht herausgibt."""
    if not v:
        return None
    s = str(v).strip()
    return None if s in ("---", "") else s
