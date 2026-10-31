"""Bildbeschreibung für /v1/document/convert IM BRIDGE-WORKER (Bau C, 31.10.2026).

Betreiberregel Rafael R3: Energy-Bildbeschreibungen (``X-App-ID: werking-energy``,
``promotional_only``) nur aus dem Promotional-Pool. Der GPU-Privacy-Dienst
erreicht den Pool-Koordinator nicht (Tailnet-ACL); der Worker schon
(``ANTHROPIC_CREDIT_POOL_URL`` → platform-api im Docker-Netz). Deshalb:

1. Der Proxy schickt ``describe_images=false`` an den Privacy-Dienst (nur Docling).
2. Dieses Modul nimmt ``images`` aus dessen Antwort und ruft dieselbe Triage und
   Beschreibung (``image_triage.plan_image_descriptions`` /
   ``image_describer.describe_images``) hier im Worker auf. Der ``VisionProvider``
   des Workers geht bei aktivem Pool immer über ``credit_pool.post_messages`` und
   nie über ``ANTHROPIC_VISION_API_KEY``.
3. Es fügt die Beschreibungen wie der Privacy-Dienst ins Markdown ein und attestiert
   ``vision_funding: "credit_pool"`` nur, wenn jeder Aufruf eine Pool-Reservierung trägt.

Laute Grenzen (kein Marker, keine stille Teilantwort, nie der bezahlte Schlüssel):
- Pool im Worker aus → 409, bevor irgendein Vision-Aufruf entsteht.
- Ein Bild > ``BRIDGE_DOCCONVERT_VISION_MAX_IMAGE_MB`` (Base64-Länge, Standard 5 =
  Anthropic-Grenze je Bild) oder alle Bilder zusammen >
  ``BRIDGE_DOCCONVERT_VISION_MAX_TOTAL_MB`` (Standard 128) → 413 vor jedem Aufruf.
- Zeitgrenze: Konvertierung + Beschreibung zusammen innerhalb der Proxy-Frist
  (``deadline``). Vorab-Schätzung je Stufe mit ``BRIDGE_DOCCONVERT_VISION_S_PER_CALL``
  (Standard 30 s je Aufruf, Annahme) × Runden bei 4 parallelen Aufrufen; passt sie
  nicht → 504 vor der Stufe. Harte Frist darüber → 504, laufende Aufrufe abgebrochen.
- Pool-/Provider-``HTTPException`` (429/503/4xx) → Status 1:1.
- Trennt der Aufrufer die Verbindung, wird die Beschreibung abgebrochen (keine
  weitere Pool-Ausgabe für eine Antwort, die niemand mehr liest).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

logger = logging.getLogger(__name__)

NOT_POOL_FUNDED = "promotional_only: document vision not pool-funded; no paid fallback"

# Gleiche Parallelität wie describe_images/select_relevant_images (Standard 4).
_PARALLEL = 4
_DISCONNECT_POLL_S = 1.0


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        # Kein stiller Rückfall auf den Standard: eine kaputte Grenze ist ein Deploy-Fehler.
        raise HTTPException(500, f"{name} ist keine Zahl") from exc
    if value <= 0:
        raise HTTPException(500, f"{name} muss > 0 sein")
    return value


def _error(status: int, message: str) -> Tuple[int, Dict[str, Any], None]:
    return status, {"status": "error", "error": message}, None


def _runden(n: int) -> int:
    return math.ceil(n / _PARALLEL) if n > 0 else 0


async def _watch_disconnect(request) -> str:
    """Endet, wenn der Aufrufer die Verbindung trennt. Ohne Empfangskanal: läuft
    nicht (laut geloggt), die harte Frist gilt weiter."""
    if request is None:
        await asyncio.Event().wait()
    while True:
        try:
            if await request.is_disconnected():
                return "disconnected"
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"docconvert worker vision: Trennungsprüfung nicht verfügbar ({exc}); nur Frist")
            await asyncio.Event().wait()
        await asyncio.sleep(_DISCONNECT_POLL_S)


async def describe_in_worker(
    payload: Dict[str, Any],
    fields: Dict[str, str],
    filename: str,
    deadline: float,
    request=None,
) -> Tuple[int, Dict[str, Any], Optional[List[dict]]]:
    """Beschreibt ``payload["images"]`` über den Pool. Rückgabe ``(status, body, usage)``.

    ``deadline`` ist ``time.monotonic()``-basiert (Ende der Proxy-Frist).
    ``usage`` ist die ehrliche Liste je Vision-Aufruf (nur bei 200), sonst None.
    """
    from src.providers import credit_pool

    images = payload.get("images") or {}
    if not isinstance(images, dict) or not images:
        return 200, payload, None

    if not credit_pool.enabled():
        logger.error(f"docconvert worker vision: {len(images)} Bilder, Credit-Pool im Worker aus → 409")
        return _error(409, NOT_POOL_FUNDED)

    try:
        max_image = int(_env_float("BRIDGE_DOCCONVERT_VISION_MAX_IMAGE_MB", 5.0) * 2**20)
        max_total = int(_env_float("BRIDGE_DOCCONVERT_VISION_MAX_TOTAL_MB", 128.0) * 2**20)
        s_per_call = _env_float("BRIDGE_DOCCONVERT_VISION_S_PER_CALL", 30.0)
    except HTTPException as exc:
        logger.error(f"docconvert worker vision: Konfigurationsfehler: {exc.detail}")
        return _error(exc.status_code, str(exc.detail))

    bad = [n for n, b in images.items() if not isinstance(b, str) or not b]
    if bad:
        return _error(502, f"document vision: {len(bad)} Bilder der Konvertierung ohne Base64-Inhalt")
    too_big = sorted(n for n, b in images.items() if len(b) > max_image)
    if too_big:
        logger.error(f"docconvert worker vision: {len(too_big)} Bilder über {max_image} Byte → 413")
        return _error(413, (
            f"document vision: {len(too_big)} Bild(er) über {max_image / 2**20:g} MB "
            f"(Base64) je Bild, z. B. {too_big[0]}; keine Beschreibung gestartet"
        ))
    total = sum(len(b) for b in images.values())
    if total > max_total:
        logger.error(f"docconvert worker vision: Bilder gesamt {total} Byte > {max_total} → 413")
        return _error(413, (
            f"document vision: Bilder gesamt {total / 2**20:.1f} MB über "
            f"{max_total / 2**20:g} MB (Base64); keine Beschreibung gestartet"
        ))

    from src.privacy_service.image_describer import (
        append_descriptions_to_markdown,
        describe_images,
    )
    from src.privacy_service.image_triage import (
        _triage_batch_size,
        plan_image_descriptions,
        should_triage,
        split_figures_and_pages,
    )

    describe_prompt = fields.get("describe_prompt") or ""
    triage_prompt = fields.get("triage_prompt") or ""
    funding_log: List[dict] = []

    def remaining() -> float:
        return deadline - time.monotonic()

    figures, _rendered = split_figures_and_pages(images)
    triage_calls = _runden(math.ceil(len(figures) / _triage_batch_size())) if should_triage(figures) else 0
    # Vorab: Triage plus mindestens eine Beschreibungsrunde muss in die Restfrist passen.
    need = (triage_calls + 1) * s_per_call
    if need > remaining():
        logger.error(
            f"docconvert worker vision: Restfrist {remaining():.0f}s < Schätzung {need:.0f}s "
            f"({len(images)} Bilder) → 504 vor dem ersten Aufruf"
        )
        return _error(504, (
            f"document vision: Restfrist {max(0.0, remaining()):.0f} s reicht nicht für "
            f"{len(images)} Bilder (Schätzung {need:.0f} s bei {s_per_call:g} s je Aufruf); "
            "keine Beschreibung gestartet"
        ))

    async def _lauf():
        to_describe, skipped, triage_meta = await plan_image_descriptions(
            images, triage_prompt=triage_prompt, promotional_only=True, funding_log=funding_log,
        )
        need_describe = _runden(len(to_describe)) * s_per_call
        if need_describe > remaining():
            raise _Frist(
                f"document vision: Restfrist {max(0.0, remaining()):.0f} s reicht nicht für "
                f"{len(to_describe)} Beschreibungen (Schätzung {need_describe:.0f} s bei "
                f"{s_per_call:g} s je Aufruf); Beschreibung nicht gestartet"
            )
        descriptions = await describe_images(
            to_describe, context=filename, describe_prompt=describe_prompt,
            promotional_only=True, funding_log=funding_log,
        )
        return to_describe, skipped, triage_meta, descriptions

    lauf = asyncio.ensure_future(_lauf())
    wache = asyncio.ensure_future(_watch_disconnect(request))
    try:
        done, _ = await asyncio.wait(
            {lauf, wache}, timeout=max(0.0, remaining()), return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        wache.cancel()
    if lauf not in done:
        lauf.cancel()
        await asyncio.gather(lauf, return_exceptions=True)
        if wache in done:
            logger.error("docconvert worker vision: Aufrufer getrennt → Beschreibung abgebrochen")
            return _error(499, "document vision: Aufrufer hat die Verbindung getrennt; Beschreibung abgebrochen")
        logger.error("docconvert worker vision: Proxy-Frist abgelaufen → 504, Aufrufe abgebrochen")
        return _error(504, "document vision: Proxy-Frist während der Bildbeschreibung abgelaufen; abgebrochen")

    try:
        to_describe, skipped, triage_meta, descriptions = lauf.result()
    except _Frist as exc:
        logger.error(f"docconvert worker vision: {exc}")
        return _error(504, str(exc))
    except HTTPException as exc:
        # Pool 429/503, Koordinator 409, Provider-4xx: Status 1:1, kein Marker.
        logger.error(f"docconvert worker vision: {exc.status_code} {exc.detail}")
        return _error(exc.status_code, str(exc.detail))
    except ValueError as exc:
        logger.error(f"docconvert worker vision: Fehlkonfiguration: {exc}")
        return _error(500, str(exc))
    except Exception as exc:  # noqa: BLE001 — laut, nie still
        logger.error(f"docconvert worker vision: {type(exc).__name__}: {exc}", exc_info=True)
        return _error(502, f"document vision failed: {type(exc).__name__}: {exc}")

    unfunded = [e for e in funding_log if not (e.get("credit_meta") or {}).get("credit_pool_reservation")]
    if unfunded:
        logger.error(f"docconvert worker vision: {len(unfunded)} Vision-Aufrufe ohne Pool-Nachweis → 409")
        return _error(409, NOT_POOL_FUNDED)
    if to_describe and not any(e.get("kind") == "describe" for e in funding_log):
        # Jede Beschreibung ist am Anbieter gescheitert (5xx/unlesbar → Marker):
        # kein Dokument „mit Bildbeschreibungen“, das keine einzige enthält.
        logger.error(f"docconvert worker vision: 0 von {len(to_describe)} Beschreibungen gelungen → 502")
        return _error(502, f"document vision: keine der {len(to_describe)} Bildbeschreibungen gelang")

    for name, label in skipped.items():
        descriptions[name] = f"_[nicht detailliert analysiert — {label}]_"
    body = dict(payload)
    body["markdown"] = append_descriptions_to_markdown(body.get("markdown") or "", descriptions)
    body["image_descriptions"] = descriptions
    if triage_meta:
        body["described_count"] = len(to_describe)
        body["triage"] = triage_meta
    body["vision_funding"] = "credit_pool"
    return 200, body, funding_log


class _Frist(Exception):
    """Vorab-Schätzung nach der Triage passt nicht in die Restfrist."""
