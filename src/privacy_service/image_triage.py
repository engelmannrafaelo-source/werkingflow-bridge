"""
Relevance triage for converted-document images.

A single PDF can carry dozens or hundreds of embedded figures. Describing every
one through Vision (see ``image_describer.describe_images``) is slow and costly
and mostly wasted on logos, watermarks and decorative photos. This module runs a
cheap **triage** pass first: it shows the extracted figures as small thumbnails
to a fast model, which returns — per image — whether it is worth a full
description and a short category label. Only the images it keeps go through the
expensive per-image description; the rest stay VISIBLE in the document as a short
label so nothing is silently dropped.

Design (backward-compatible by construction):
- The convert response shape is unchanged. Triage only changes WHICH of the
  already-extracted images get a full description. Below the threshold, behaviour
  is byte-for-byte the old "describe every image".
- ``page-NNN.png`` render images (whole scan/oversize pages the converter
  deliberately rasterised for Vision) are NEVER triaged away — they are
  high-value by construction. Triage only ranks Docling-extracted figures.
- Criteria are caller-supplied (``triage_prompt``), mirroring ``describe_prompt``
  on the describe path, because the Bridge convert is shared across apps.

Failure policy (defensive, no silent fail, no silent drop):
- A triage failure (parse error, API error, misconfiguration) falls back to
  describing ALL images — i.e. exactly today's behaviour. A triage bug can never
  make a figure disappear.
- Any figure the model does not rule on explicitly is KEPT (inclusion bias).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from src.vision_provider import get_vision_provider
from src.model_registry import get_default_model
from src.privacy_service.document_converter import _downscale_b64_png

logger = logging.getLogger("privacy-service.image-triage")

# Rendered whole-page images use this filename prefix (see document_converter).
_RENDERED_PAGE_PREFIX = "page-"

# Thumbnails shown to the triage model — small on purpose: enough to tell a
# schematic from a logo, cheap on tokens.
_TRIAGE_THUMB_EDGE = 384

_DEFAULT_THRESHOLD = 15
_DEFAULT_BATCH_SIZE = 20

_TRIAGE_SYSTEM_PROMPT = (
    "Du bist eine Vorauswahl-Stufe in einem Dokument-Konverter. Ein Dokument wird "
    "gerade in Text umgewandelt, damit nachgelagerte, rein textbasierte Schritte "
    "damit arbeiten können; die enthaltenen Abbildungen wurden einzeln herausgelöst "
    "und liegen dir als kleine Vorschaubilder vor. Jedes Bild, das du auswählst, "
    "wird im nächsten Schritt ausführlich in Text beschrieben — diese Beschreibung "
    "ersetzt das Bild im Dokument, denn die folgenden Schritte sehen nur noch den "
    "Text. Nicht ausgewählte Bilder bleiben als kurzer Platzhalter erhalten, ihr "
    "Inhalt geht dem Text aber verloren.\n\n"
    "Entscheide deshalb je Bild: Trägt es eigenständige Information, die den Inhalt "
    "des Dokuments für einen Leser ausmacht, der nur den Text vor sich hat? Dann "
    "auswählen. Bilder, die keinen Sachinhalt beitragen, sondern der Gestaltung oder "
    "der Wiedererkennung dienen, brauchen keine ausführliche Beschreibung. Im "
    "Zweifel auswählen — ein übersehenes informationstragendes Bild fehlt dem Text "
    "dauerhaft, ein unnötig beschriebenes kostet nur einen zusätzlichen Schritt. "
    "Antworte ausschließlich mit JSON."
)


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on") if value is not None else False


def _triage_enabled() -> bool:
    """Master kill-switch (env). Default ON — the whole point is app-wide benefit."""
    return _truthy(os.getenv("BRIDGE_IMAGE_TRIAGE_ENABLED", "true"))


def _triage_threshold() -> int:
    """Figure count above which triage kicks in. Below it: describe all (unchanged)."""
    try:
        return int(os.getenv("BRIDGE_IMAGE_TRIAGE_THRESHOLD", str(_DEFAULT_THRESHOLD)))
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD


def _triage_batch_size() -> int:
    try:
        return max(1, int(os.getenv("BRIDGE_IMAGE_TRIAGE_BATCH", str(_DEFAULT_BATCH_SIZE))))
    except (TypeError, ValueError):
        return _DEFAULT_BATCH_SIZE


def split_figures_and_pages(images: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Split the converter's image dict into ``(figures, rendered_pages)``.

    Rendered pages (``page-NNN.png``) are a deliberate converter decision and are
    never triaged away; only Docling-extracted figures are candidates for triage.
    """
    figures = {k: v for k, v in images.items() if not k.startswith(_RENDERED_PAGE_PREFIX)}
    rendered = {k: v for k, v in images.items() if k.startswith(_RENDERED_PAGE_PREFIX)}
    return figures, rendered


def should_triage(figures: Dict[str, str]) -> bool:
    """True iff triage is enabled and there are enough figures to be worth it."""
    return _triage_enabled() and len(figures) > _triage_threshold()


def _batch_instruction(n: int) -> str:
    return (
        f"Hier sind {n} Bilder aus dem Dokument (Bild 1 bis {n}). Entscheide je Bild, "
        "ob es eine ausführliche Textbeschreibung verdient. Gib AUSSCHLIESSLICH ein "
        "JSON-Objekt zurück, ein Eintrag pro Bildnummer, im Format "
        '{"1": {"relevant": true, "label": "<kurze Kategorie>"}, '
        '"2": {"relevant": false, "label": "<kurze Kategorie>"}}. '
        "label = knappe Benennung dessen, was das Bild zeigt (max. 3 Wörter). Im "
        "Zweifel relevant: true."
    )


def _parse_verdict(text: str) -> Dict[str, dict]:
    """Extract the ``{index: {relevant, label}}`` object from a model reply.

    Robust to code fences and surrounding prose. Raises ``ValueError`` if no JSON
    object is present — the caller maps that to "keep the whole batch" (no drop).
    """
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in triage response: {text[:120]!r}")
    obj = json.loads(s[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("triage JSON is not an object")
    return obj


async def _triage_one_batch(
    provider, batch: List[Tuple[str, str]], model: str, system_prompt: str
) -> Dict[str, dict]:
    """Triage one batch of ``(name, b64)`` figures → ``{name: {relevant, label}}``.

    On any non-config failure the whole batch is kept relevant (never dropped).
    A misconfiguration (missing vision key → ValueError) propagates so the caller
    falls back to describe-all and the loud error surfaces there.
    """
    content: List[dict] = [{"type": "text", "text": _batch_instruction(len(batch))}]
    for idx, (_name, b64) in enumerate(batch, start=1):
        thumb = _downscale_b64_png(b64, max_edge=_TRIAGE_THUMB_EDGE)
        content.append({"type": "text", "text": f"Bild {idx}:"})
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{thumb}"}}
        )
    messages = [{"role": "user", "content": content}]

    try:
        resp = await provider.analyze(
            messages,
            model=model,
            max_tokens=1200,
            temperature=0.0,
            system_prompt=system_prompt,
        )
        parsed = _parse_verdict(resp.content or "")
    except ValueError as e:
        # Misconfiguration from the provider (no vision key) propagates; a JSON
        # parse ValueError from _parse_verdict does NOT (it has no such message).
        if "JSON" not in str(e) and "triage response" not in str(e):
            raise
        logger.warning(f"[image_triage] unparseable batch reply, keeping all relevant: {e}")
        parsed = {}
    except Exception as e:  # noqa: BLE001 — never let a batch error drop images
        logger.warning(f"[image_triage] batch failed, keeping all relevant: {e}")
        parsed = {}

    out: Dict[str, dict] = {}
    for idx, (name, _b64) in enumerate(batch, start=1):
        v = parsed.get(str(idx)) or {}
        out[name] = {
            "relevant": bool(v.get("relevant", True)),  # inclusion bias
            "label": (str(v.get("label") or "")).strip(),
        }
    return out


async def select_relevant_images(
    figures: Dict[str, str],
    *,
    triage_prompt: str = "",
    batch_size: Optional[int] = None,
    model: Optional[str] = None,
    max_concurrency: int = 4,
) -> Dict[str, dict]:
    """Return ``{name: {"relevant": bool, "label": str}}`` for every figure.

    Batches the figures and runs one cheap Vision call per batch (bounded
    concurrency). ``triage_prompt`` overrides the app-neutral system prompt.
    """
    if not figures:
        return {}

    batch_size = batch_size or _triage_batch_size()
    model = model or get_default_model("haiku").id
    provider = get_vision_provider()
    system_prompt = triage_prompt or _TRIAGE_SYSTEM_PROMPT

    items = list(figures.items())
    batches = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]
    sem = asyncio.Semaphore(max_concurrency)

    async def run_batch(batch: List[Tuple[str, str]]) -> Dict[str, dict]:
        async with sem:
            return await _triage_one_batch(provider, batch, model, system_prompt)

    verdicts = await asyncio.gather(*(run_batch(b) for b in batches))
    merged: Dict[str, dict] = {}
    for v in verdicts:
        merged.update(v)
    return merged


async def plan_image_descriptions(
    images: Dict[str, str],
    *,
    triage_prompt: str = "",
) -> Tuple[Dict[str, str], Dict[str, str], Optional[dict]]:
    """Decide which images to fully describe.

    Returns ``(to_describe, skipped_labels, triage_meta)``:
    - ``to_describe``: the ``{name: b64}`` subset to run through Vision.
    - ``skipped_labels``: ``{name: label}`` for figures triaged out (kept VISIBLE
      in the document by the caller as a short marker — never silently dropped).
    - ``triage_meta``: summary dict, or ``None`` when no triage happened (in which
      case ``to_describe == images`` and the response stays byte-identical).

    Fail-safe: any triage failure falls back to describing ALL images.
    """
    figures, rendered = split_figures_and_pages(images)
    if not should_triage(figures):
        return dict(images), {}, None

    try:
        verdict = await select_relevant_images(figures, triage_prompt=triage_prompt)
    except Exception as e:  # noqa: BLE001 — triage must never break conversion
        logger.warning(
            f"[image_triage] triage failed ({e}); describing all {len(figures)} figures.",
            exc_info=True,
        )
        return dict(images), {}, None

    # Keep figures the model marked relevant, plus any it did not rule on at all.
    unresolved = set(figures) - set(verdict)
    keep = {k for k, v in verdict.items() if v.get("relevant", True)} | unresolved

    to_describe = {**rendered, **{k: figures[k] for k in figures if k in keep}}
    skipped_labels = {
        k: (verdict.get(k, {}).get("label") or "übersprungen")
        for k in figures
        if k not in keep
    }
    meta = {
        "enabled": True,
        "total_figures": len(figures),
        "described_figures": len(figures) - len(skipped_labels),
        "rendered_pages": len(rendered),
        "skipped": skipped_labels,
    }
    return to_describe, skipped_labels, meta
