"""Perplexity as a client tool of the research-cloud executor.

Rafael 2026-10-02 (Bühne b-hey-chad-schammer-ich-habe-in-letzter-ze-20261002,
answer a): Perplexity becomes a tool of the whole bridge research. Basis: the
Mühl comparison of 22 research questions (18 better, 2 equal, 1 worse, 1 none) —
local-storage/perplexity-vergleich-muehl-20261002/VERGLEICH.md.

Flag-gated off by default (RESEARCH_PERPLEXITY_ENABLED), same shape as the
library (library.py):

- switched on but without a key -> PerplexityUnavailableError, the run is
  refused before the first token (loud, never "quietly without Perplexity");
- one failing call -> PerplexityCallError, which the executor turns into a
  fail-soft tool_result(is_error=True) so the model continues with web_search.

The query the model writes goes to a third-party service. It never leaves this
process unanonymized: the executor routes it through the same fail-closed gate
as the research prompt (anonymize_gate.py) BEFORE ask_perplexity is called.
This module itself has no anonymization — it trusts its caller for that, and
the executor is the only caller.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from os import environ
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PERPLEXITY_TOOL_NAME = "perplexity_search"
PERPLEXITY_API_URL = "https://api.perplexity.ai/v1/responses"
PRESETS = ("fast", "low", "medium", "high", "xhigh")

PERPLEXITY_TOOL: Dict[str, Any] = {
    "name": PERPLEXITY_TOOL_NAME,
    "description": (
        "Web-Recherche über Perplexity: beantwortet eine präzise Frage aus vielen Webquellen "
        "und liefert die Antwort mit nummerierter Quellenliste (nr, titel, url). Gut, um "
        "schnell die einschlägigen Primärquellen zu finden (Normen, Gesetze, Hersteller-"
        "Datenblätter, Behördenseiten) und den Stand einer Frage zu überblicken.\n"
        "Die Antwort ist eine Zusammenfassung Dritter und allein KEIN Beleg: Jede Zahl oder "
        "Vorgabe, die der Bericht tragen soll, liest du mit web_fetch an der Originalquelle "
        "nach (Norm, Gesetz, Hersteller-PDF) und zitierst die Originalquelle, nicht Perplexity.\n"
        "Die kuratierte Bibliothek bleibt die erste Quelle; nutze dieses Werkzeug für das, "
        "was dort fehlt. Formuliere die Frage sachlich und ohne Namen von Personen oder Firmen "
        "des Auftraggebers — sie wird vor dem Senden anonymisiert."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "frage": {
                "type": "string",
                "description": "Eine konkrete Recherchefrage (ein Thema je Aufruf).",
            }
        },
        "required": ["frage"],
    },
}


class PerplexityUnavailableError(Exception):
    """Switched ON but not usable (no key, unknown preset). Aborts the run
    before any model tokens are spent — the loud kind."""


class PerplexityCallError(Exception):
    """One call failed (HTTP error after retries, incomplete answer). The
    executor turns this into a fail-soft tool_result — the fail-soft kind."""


class PerplexityConfig(BaseModel):
    enabled: bool = False
    # repr=False: a config object printed into a log line must never carry the key.
    api_key: Optional[str] = Field(default=None, repr=False)
    preset: str = "medium"
    timeout_seconds: float = 300.0
    # Retries after 429/5xx on top of the first attempt. The CLI tool measured
    # 6 x 429 out of 22 parallel calls (Mühl run, 29.09.2026).
    max_retries: int = 4


def load_perplexity_config() -> PerplexityConfig:
    return PerplexityConfig(
        enabled=environ.get("RESEARCH_PERPLEXITY_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"),
        api_key=environ.get("PERPLEXITY_API_KEY") or None,
        preset=(environ.get("RESEARCH_PERPLEXITY_PRESET") or "medium").strip(),
    )


def perplexity_enabled(config: PerplexityConfig) -> bool:
    """True iff the flag is on. Validity is NOT part of this answer — a flag
    that is on with a missing key must fail loudly (check_perplexity_usable),
    not quietly drop the tool as the library once did."""
    return config.enabled


def check_perplexity_usable(config: PerplexityConfig) -> None:
    if not config.enabled:
        return
    if not config.api_key:
        raise PerplexityUnavailableError(
            "RESEARCH_PERPLEXITY_ENABLED is on, but PERPLEXITY_API_KEY is not set — "
            "values come from Infisical (dev-server/dev) via sync-infisical-to-bridge"
        )
    if config.preset not in PRESETS:
        raise PerplexityUnavailableError(
            f"RESEARCH_PERPLEXITY_PRESET={config.preset!r} is not one of {PRESETS}"
        )


class PerplexitySource(BaseModel):
    nr: Optional[int] = None
    titel: str
    url: str


class PerplexityAnswer(BaseModel):
    text: str
    quellen: List[PerplexitySource] = Field(default_factory=list)
    kosten_usd: Optional[float] = None
    model: Optional[str] = None
    warnungen: List[str] = Field(default_factory=list)


def parse_perplexity_response(body: Dict[str, Any]) -> PerplexityAnswer:
    """Turn an Agent-API response into answer text + sources.

    Every search-result id gets its source, even when its URL already appeared
    under another id — the [web:N] marks in the text point at ids, and dropping
    the duplicate left them dangling (Mühl run, question 6: 9 of 14 citations
    unresolvable; same fix as orchestrator tools/perplexity-recherche/quellen.ts).
    """
    if body.get("status") != "completed":
        err = body.get("error") or {}
        raise PerplexityCallError(
            f"Perplexity answer incomplete (status {body.get('status')!r}): "
            f"{err.get('code', '')} {err.get('message', '')}".strip()
        )
    by_nr: Dict[int, PerplexitySource] = {}
    without_nr: List[PerplexitySource] = []
    urls: set = set()
    warnungen: List[str] = []
    texts: List[str] = []
    for part in body.get("output") or []:
        kind = part.get("type")
        if kind == "search_results":
            results = part.get("results")
            if not isinstance(results, list):
                warnungen.append("search_results part without results (empty search)")
                continue
            for r in results:
                nr = r.get("id")
                if nr in by_nr:
                    if by_nr[nr].url != r.get("url"):
                        warnungen.append(f"result id {nr} twice with different URLs — kept the first")
                    continue
                src = PerplexitySource(nr=nr, titel=r.get("title") or r.get("url") or "", url=r.get("url") or "")
                by_nr[nr] = src
                urls.add(src.url)
        elif kind == "message":
            for c in part.get("content") or []:
                if c.get("type") == "output_text" and c.get("text"):
                    texts.append(c["text"])
                for a in c.get("annotations") or []:
                    url = a.get("url")
                    if a.get("type") != "url_citation" or not url or url in urls:
                        continue
                    urls.add(url)
                    without_nr.append(PerplexitySource(titel=a.get("title") or url, url=url))
    text = "".join(texts)
    known = set(by_nr)
    cited = {int(n) for n in re.findall(r"\[(?:web:)?(\d+)\]", text)}
    dangling = sorted(cited - known)
    if dangling:
        warnungen.append(f"cited sources without entry: {dangling}")
    cost = ((body.get("usage") or {}).get("cost") or {}).get("total_cost")
    return PerplexityAnswer(
        text=text,
        quellen=[by_nr[n] for n in sorted(by_nr)] + without_nr,
        kosten_usd=float(cost) if cost is not None else None,
        model=body.get("model"),
        warnungen=warnungen,
    )


_RETRYABLE = frozenset({429, 500, 502, 503, 504, 529})


def _retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    try:
        sec = float(retry_after) if retry_after else None
    except ValueError:
        sec = None
    if sec is not None and sec >= 0:
        return min(sec, 120.0)
    return min(4.0 * 2 ** (attempt - 1), 60.0) * (1 + 0.25 * random.random())


async def ask_perplexity(
    frage: str, config: PerplexityConfig, client: httpx.AsyncClient, *, sleep=asyncio.sleep
) -> PerplexityAnswer:
    """One Perplexity Agent-API call. ``frage`` MUST already be anonymized."""
    if not config.api_key:
        raise PerplexityCallError("PERPLEXITY_API_KEY missing")
    body = {"input": frage, "preset": config.preset, "stream": False}
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    for attempt in range(config.max_retries + 1):
        try:
            resp = await client.post(PERPLEXITY_API_URL, json=body, headers=headers, timeout=config.timeout_seconds)
        except httpx.HTTPError as e:
            raise PerplexityCallError(f"Perplexity request failed: {type(e).__name__}: {e}") from e
        if resp.status_code == 200:
            return parse_perplexity_response(resp.json())
        if resp.status_code in _RETRYABLE and attempt < config.max_retries:
            delay = _retry_delay(attempt + 1, resp.headers.get("retry-after"))
            logger.warning(
                f"perplexity: HTTP {resp.status_code}, retry {attempt + 1}/{config.max_retries} in {delay:.0f}s"
            )
            await sleep(delay)
            continue
        raise PerplexityCallError(f"Perplexity HTTP {resp.status_code}: {resp.text[:300]}")
    raise AssertionError("unreachable")  # pragma: no cover


def format_tool_result_text(answer: PerplexityAnswer) -> str:
    lines = [answer.text.strip(), "", "Quellen (Perplexity-Treffer, [web:N] im Text = nr):"]
    for q in answer.quellen:
        lines.append(f"- [{q.nr if q.nr is not None else '-'}] {q.titel} — {q.url}")
    lines.append("")
    lines.append(
        "Hinweis: Diese Antwort ist kein Beleg. Tragende Zahlen mit web_fetch an der "
        "Originalquelle nachlesen und die Originalquelle zitieren."
    )
    return "\n".join(lines)
