"""
Vision-based image description for converted documents.

Docling extracts figures/charts/photos from a PDF as base64 PNGs and references
them in the Markdown, but it does NOT describe them. This module runs each
extracted image through the Bridge ``VisionProvider`` so a converted document
carries textual descriptions of its images. That way every consumer (Report,
Energy, ...) gets "PDF → Markdown incl. image descriptions" for free instead of
each app re-implementing image→Vision on its own.

App-neutral by design: the prompt describes the image factually for downstream
text processing and makes no assumption about a specific report format.

Failure policy (defensive, no silent fail):
- A per-image Vision failure becomes a VISIBLE error marker in that image's
  description — one bad figure must not sink a whole multi-page document.
- A completely missing ANTHROPIC_VISION_API_KEY (provider raises ValueError on
  the first call) PROPAGATES: the caller explicitly asked for descriptions and
  the service is misconfigured — fail loud, do not pretend it worked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Tuple

from src.vision_provider import get_vision_provider

logger = logging.getLogger(__name__)

# Current Claude vision model (matches VisionProvider's documented default).
DEFAULT_VISION_MODEL = "claude-sonnet-4-5-20250929"

_SYSTEM_PROMPT = (
    "Du beschreibst Bilder aus Fachdokumenten (Diagramme, Schemata, Grafiken, "
    "Tabellen-Screenshots, Fotos) sachlich und vollständig für die "
    "Weiterverarbeitung ohne das Originalbild. Gib ausschließlich die "
    "Beschreibung als Markdown zurück."
)

_USER_PROMPT = (
    "Beschreibe dieses Bild so, dass ein nachgelagerter Textverarbeitungs-Prozess "
    "ohne das Bild damit arbeiten kann. Wähle die Struktur passend zum Bildtyp. "
    "Übernimm Zahlen, Achsen, Legenden und Beschriftungen WORTGETREU, erfinde "
    "nichts. Nur Markdown."
)


async def _describe_one(
    provider, b64: str, model: str, context: str, describe_prompt: str = ""
) -> str:
    # A caller-supplied describe_prompt REPLACES the app-neutral default system
    # prompt (per-app override). Without it, behaviour is byte-for-byte the
    # neutral default. The user message (incl. the "übernimm wortgetreu, erfinde
    # nichts / nur Markdown" guardrails) is unchanged either way.
    system_prompt = describe_prompt if describe_prompt else _SYSTEM_PROMPT
    user_text = _USER_PROMPT
    if context:
        user_text += f"\n\nKontext (Dateiname/Umgebung): {context}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
            ],
        }
    ]
    resp = await provider.analyze(
        messages,
        model=model,
        max_tokens=1500,
        temperature=0.1,
        system_prompt=system_prompt,
    )
    return (resp.content or "").strip()


async def describe_images(
    images: Dict[str, str],
    *,
    context: str = "",
    model: str = DEFAULT_VISION_MODEL,
    max_concurrency: int = 4,
    describe_prompt: str = "",
) -> Dict[str, str]:
    """Return ``{filename: description}`` for every extracted image.

    One Vision call per image, bounded concurrency. See module docstring for the
    failure policy (per-image = visible marker; missing API key = propagate).

    ``describe_prompt`` is an optional per-app override for the system prompt.
    Empty (the default) keeps the app-neutral factual prompt, so existing
    callers are unaffected.
    """
    if not images:
        return {}

    provider = get_vision_provider()
    sem = asyncio.Semaphore(max_concurrency)

    async def run(name: str, b64: str) -> Tuple[str, str]:
        async with sem:
            try:
                return name, await _describe_one(
                    provider, b64, model, context, describe_prompt
                )
            except ValueError:
                # Misconfiguration (e.g. no ANTHROPIC_VISION_API_KEY) — surface loudly.
                raise
            except Exception as e:  # per-image failure: visible, never silently dropped
                logger.error(
                    f"[image_describer] Vision failed for {name}: {e}", exc_info=True
                )
                return name, f"_[Bildbeschreibung fehlgeschlagen: {e}]_"

    pairs = await asyncio.gather(*(run(n, b) for n, b in images.items()))
    return dict(pairs)


def append_descriptions_to_markdown(
    markdown: str, descriptions: Dict[str, str]
) -> str:
    """Append a ``## Bildbeschreibungen`` section listing each image's description."""
    if not descriptions:
        return markdown
    parts = [markdown.rstrip(), "", "## Bildbeschreibungen", ""]
    for name, desc in descriptions.items():
        parts.append(f"### {name}")
        parts.append("")
        parts.append(desc)
        parts.append("")
    return "\n".join(parts)
