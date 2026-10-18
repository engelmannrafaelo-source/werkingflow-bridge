"""Nachholauftrag fuer Planwechsel (Migration 070).

Ein Wechsel steht auf 'neu_aktiv' (Kuendigung des alten Abos steht aus) oder
'doppelt_aktiv' (Kuendigung gescheitert, Alarm ausgeloest). Mollie wiederholt
den Webhook nur begrenzt; dieser Lauf wiederholt den Abschluss periodisch, bis
er gelingt oder ein Mensch eingreift. Jeder Fehlschlag bleibt laut
(billing_service._wechsel_abschliessen loggt ALARM, Status bleibt sichtbar unter
GET /v1/billing/abo-abschluesse).
"""
from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

NACHHOL_TAKT_S = int(os.getenv("BRIDGE_ABO_WECHSEL_NACHHOL_TAKT_S", "900"))


async def _nachhol_schleife() -> None:
    from src.billing.billing_service import wechsel_kuendigungen_nachholen

    while True:
        await asyncio.sleep(NACHHOL_TAKT_S)
        try:
            bericht = await wechsel_kuendigungen_nachholen()
        except Exception:  # noqa: BLE001 — Schleife muss weiterlaufen; Fehler laut im Log
            logger.exception("[ALARM][abo-wechsel] Nachholauftrag gescheitert")
            continue
        if bericht["geprueft"]:
            log = logger.error if bericht["doppeltAktiv"] else logger.info
            log("[abo-wechsel] Nachholauftrag: %s", bericht)


def start_abo_wechsel_nachhol_loop() -> asyncio.Task:
    """Einmal beim Start der Platform-API aufrufen."""
    if NACHHOL_TAKT_S <= 0:
        raise ValueError("BRIDGE_ABO_WECHSEL_NACHHOL_TAKT_S muss > 0 sein")
    return asyncio.create_task(_nachhol_schleife())
