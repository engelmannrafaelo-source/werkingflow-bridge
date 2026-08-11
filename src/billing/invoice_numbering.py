"""
Rechnungsnummern-Vergabe — getrennte Nummernkreise pro Produkt.

§ 11 Abs 1 Z 3 lit. h UStG 1994 verlangt "eine fortlaufende Nummer mit einer
oder MEHREREN Zahlenreihen, die zur Identifizierung der Rechnung EINMALIG
vergeben wird". Getrennte Nummernkreise (pro Produkt/Belegart) sind damit
ausdruecklich zulaessig — jeder Kreis muss fuer sich fortlaufend und jede
Nummer einmalig sein. Lueckenlosigkeit ist NICHT gefordert (Luecken durch
Storno/Rollback sind erklaerungsbeduerftig, aber zulaessig).

Umsetzung: eine eigene Postgres-Sequenz je (Praefix, Jahr). Format
<PRAEFIX>-<Jahr>-<5-stellig>, z. B. WR-2026-00001.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# app_id -> Rechnungsnummern-Praefix. Ein eigener Nummernkreis pro Produkt.
# Neues Produkt -> hier ergaenzen (sonst greift der Fallback-Kreis + Warn-Log).
_APP_PREFIX: dict[str, str] = {
    "werking-report": "WR",
    "werking-energy": "WE",
    "werking-noise": "WN",
    "werking-check": "WC",
    "engelmann": "EG",
}

# Praefix fuer kontobezogene (nicht produkt-spezifische) Belege.
TOPUP_PREFIX = "TU"  # API-Guthaben-Aufladung — gilt kontoweit, keinem Produkt zuordenbar

# Fallback fuer unbekannte/fehlende app_id. Behaelt bewusst den historischen
# Praefix "INV" UND die historische Sequenz (siehe _seq_name), damit bereits
# vergebene INV-Nummern nicht erneut erzeugt werden (invoice_number ist UNIQUE).
FALLBACK_PREFIX = "INV"

# 1-6 Großbuchstaben: erlaubt Produkt-Präfixe (WR/WE/…) UND die Beta-Serie "B".
_PREFIX_RE = re.compile(r"^[A-Z]{1,6}$")


def prefix_for_app(app_id: str | None) -> str:
    """app_id -> Nummernkreis-Praefix.

    Unbekannte/fehlende app_id -> FALLBACK_PREFIX mit Warn-Log. Wirft NIE:
    eine Rechnung muss immer erzeugt werden koennen (die Zahlung ist passiert,
    der Beleg MUSS existieren). Ein unbekanntes Produkt landet daher sauber im
    Fallback-Kreis statt die Rechnungserzeugung zu blockieren.
    """
    if not app_id:
        return FALLBACK_PREFIX
    prefix = _APP_PREFIX.get(app_id)
    if prefix is None:
        logger.warning(
            "invoice_numbering: kein Nummernkreis-Praefix fuer app_id=%r — "
            "nutze Fallback %r. Neues Produkt? _APP_PREFIX ergaenzen.",
            app_id, FALLBACK_PREFIX,
        )
        return FALLBACK_PREFIX
    return prefix


def _seq_name(prefix: str, year: int) -> str:
    """Postgres-Sequenzname je (Praefix, Jahr).

    Der Fallback-Praefix "INV" nutzt die HISTORISCHE Sequenz `invoice_seq_<year>`
    (ohne Praefix-Infix), damit die vor dieser Umstellung vergebenen INV-Nummern
    nahtlos fortgesetzt werden und keine Kollision mit `invoice_number` (UNIQUE)
    entsteht. Alle Produkt-Kreise bekommen eine frische eigene Sequenz.
    """
    if prefix == FALLBACK_PREFIX:
        return f"invoice_seq_{year}"
    return f"invoice_seq_{prefix.lower()}_{year}"


async def next_invoice_number(conn: Any, prefix: str) -> str:
    """Naechste Nummer im Kreis <prefix>: '<PREFIX>-<Jahr>-<5-stellig>'.

    Eigene Sequenz je (Praefix, Jahr). Sequenzen sind NICHT transaktional: eine
    einmal gezogene Nummer gilt als verbraucht (kein Re-Use bei Rollback) — das
    ist gewollt (Einmaligkeit vor Lueckenlosigkeit).
    """
    if not _PREFIX_RE.match(prefix):
        raise ValueError(f"invoice_numbering: ungueltiges Praefix {prefix!r}")
    year = datetime.now(timezone.utc).year
    seq = _seq_name(prefix, year)
    # Praefix ist validiert (nur A-Z, 2-6 Zeichen) -> kein Injection-Vektor.
    await conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq} START 1 INCREMENT 1")
    n = await conn.fetchval(f"SELECT nextval('{seq}')")
    return f"{prefix}-{year}-{int(n):05d}"
