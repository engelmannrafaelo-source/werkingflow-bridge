"""perplexity_search on the pool path (Claude Code CLI in the worker).

Counterpart of the research-cloud client tool (src/research_cloud/perplexity.py,
Rafael 02.10.2026). The pool path has no tool loop of its own — the CLI runs
the tools — so the tool comes as an in-process SDK MCP server
(claude_code_sdk.create_sdk_mcp_server): the handler runs in THIS worker
process, the CLI only sees ``mcp__perplexity__perplexity_search``.

Why in-process and not a released shell command or a stdio MCP server:
- the PERPLEXITY_API_KEY stays in the worker; a command the model runs via Bash
  would have to carry it in the CLI's environment;
- the anonymize gate is the SAME function as on the cloud path
  (anonymize_gate.anonymize_query_for_cloud), called directly — a separate
  process would need its own way back into the privacy service;
- calls and cost are counted here and booked with the run.

Same rules as the cloud path: the query passes the fail-closed anonymize gate
before it leaves; if the gate fails, nothing is sent, every further call is
refused, and the run ends as an error (gate_error). HTTP failures are
fail-soft (the model continues with WebSearch). A budget caps the calls.

claude-code-sdk 0.0.22 drops an ``is_error`` key returned by a tool handler;
only a raised exception reaches the CLI as an error result. Hence ``handle``
raises PoolToolError for every refusal.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

from src.research_cloud.fetch_document import (
    FETCH_DOCUMENT_TOOL,
    FETCH_DOCUMENT_TOOL_NAME,
    DocumentCounters,
    DocumentFetchError,
    fetch_document,
)
from src.research_cloud.perplexity import (
    PERPLEXITY_TOOL,
    PERPLEXITY_TOOL_NAME,
    PerplexityCallError,
    PerplexityConfig,
    ask_perplexity,
    format_tool_result_text,
)

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "perplexity"
MCP_TOOL_NAME = f"mcp__{MCP_SERVER_NAME}__{PERPLEXITY_TOOL_NAME}"
# fetch_document runs in its own in-process server: the CLI shows the server
# name in the tool name, and "perplexity" would mislabel a document download.
DOC_MCP_SERVER_NAME = "dokument"
DOC_TOOL_NAME = f"mcp__{DOC_MCP_SERVER_NAME}__{FETCH_DOCUMENT_TOOL_NAME}"

POOL_PROMPT_SECTION = f"""

## Perplexity-Recherche (`{MCP_TOOL_NAME}`)

Mit dem Werkzeug `{MCP_TOOL_NAME}` (Parameter `frage`) stellst du eine konkrete Frage an eine Web-Recherche,
die viele Quellen auswertet und eine nummerierte Quellenliste zurückgibt.
Das Werkzeug steht dir direkt zur Verfügung — du musst es nicht erst suchen oder laden.
Eine kuratierte Bibliothek, falls vorhanden, bleibt die erste Quelle.

Reihenfolge für alles, was im Netz steht:
1. Hersteller-Datenblätter, Produktkennwerte (Leistung, Schalldruck, Maße, Zulassungen) und die aktuell
   gültige Ausgabe einer Norm, Richtlinie oder Verordnung suchst du ZUERST mit `{MCP_TOOL_NAME}` — eine
   Frage je Produkt bzw. je Regelwerk, mit Typbezeichnung bzw. Normnummer. WebSearch ist der Rückfall, wenn
   Perplexity nichts Brauchbares liefert oder das Budget erschöpft ist.
2. Danach liest du jede Zahl oder Vorgabe, die der Bericht trägt, an der Originalquelle nach und zitierst
   diese Originalquelle: Dokumente (PDF-Datenblatt, Broschüre, Katalog, Merkblatt) mit `{DOC_TOOL_NAME}` —
   mit Seitenzahl —, Webseiten (Normenverlag, Gesetzesstelle, Herstellerseite) mit WebFetch.

Eine Perplexity-Antwort ist kein Beleg. Was du nicht an der Quelle prüfen konntest, kennzeichnest du als
„nicht verifiziert (nur Perplexity)“.

Zweite Fundstelle statt Abbruch: Liefert eine Quelle keinen lesbaren Inhalt (Fehler, leerer Text, gesuchter
Typ fehlt), suchst du eine zweite Fundstelle derselben Angabe — Herstellerseite, andere Händler- oder
Katalogfassung, über `{MCP_TOOL_NAME}` oder WebSearch —, bevor ein Kennwert als „nicht bestätigt“ gilt.
Im Bericht nennst du dann beide versuchten Quellen.

Normlücken zuerst über Perplexity: Fehlt eine im Auftrag genannte Norm, Richtlinie oder Verordnung in der
Bibliothek, fragst du `{MCP_TOOL_NAME}` nach aktueller Ausgabe, Ausgabedatum und Anwendungsbereich, bevor
du sie als Lücke meldest. Die Lücke bleibt erlaubt, wenn auch das nichts Belegbares liefert — dann mit
dem Vermerk, dass gesucht wurde."""


class PoolToolError(Exception):
    """Raised by the handler so the SDK reports an error tool_result."""


class PoolPerplexityTool:
    """One instance per research run: holds the budget and the counters."""

    def __init__(
        self,
        config: PerplexityConfig,
        anonymize: Callable[[str], Awaitable[str]],
        client: Optional[httpx.AsyncClient],
        max_uses: int,
    ) -> None:
        self.config = config
        self.anonymize = anonymize
        self.client = client
        self.max_uses = max_uses
        self.calls = 0
        self.cost_usd = 0.0
        self.cost_missing = 0
        self.gate_error: Optional[str] = None

    async def handle(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if self.gate_error:
            raise PoolToolError(
                "perplexity_search ist für diesen Lauf gesperrt (Anonymisierung fehlgeschlagen)."
            )
        frage = str((args or {}).get("frage") or "").strip()
        if not frage:
            raise PoolToolError("perplexity_search braucht den Parameter 'frage'.")
        if self.calls >= self.max_uses:
            raise PoolToolError(
                f"perplexity_search: Budget dieser Recherche erschöpft ({self.max_uses} Aufrufe). "
                "Weiter mit WebSearch/WebFetch."
            )
        self.calls += 1
        try:
            anonymized = await self.anonymize(frage)
            if not anonymized or not anonymized.strip():
                raise ValueError("anonymize gate returned empty text")
        except Exception as e:
            # Recorded, not just answered: the caller turns this into a failed
            # run. A broken privacy path must not end as a "successful" report.
            self.gate_error = f"{type(e).__name__}: {e}"
            logger.error(f"research pool: perplexity_search anonymize gate failed — nothing sent: {e}")
            raise PoolToolError(
                "perplexity_search: Anonymisierung fehlgeschlagen — die Anfrage wurde NICHT gesendet."
            ) from e
        try:
            if self.client is not None:
                answer = await ask_perplexity(anonymized, self.config, self.client)
            else:
                # Per call: the research run has no place that would close a
                # client opened for its whole duration.
                async with httpx.AsyncClient() as c:
                    answer = await ask_perplexity(anonymized, self.config, c)
        except PerplexityCallError as e:
            logger.warning(f"research pool: perplexity_search failed (fail-soft): {e}")
            raise PoolToolError(f"Perplexity nicht erreichbar: {e}. Weiter mit WebSearch.") from e
        for w in answer.warnungen:
            logger.warning(f"research pool: perplexity_search: {w}")
        if answer.kosten_usd is None:
            self.cost_missing += 1
            logger.error(
                "research pool: perplexity answer carries no usage.cost.total_cost — "
                "this call is NOT in the booked cost"
            )
        else:
            self.cost_usd += answer.kosten_usd
        logger.info(
            f"research pool perplexity call -> ok, {len(answer.quellen)} sources, cost_usd={answer.kosten_usd}"
        )
        return {"content": [{"type": "text", "text": format_tool_result_text(answer)}]}

    def server(self) -> Dict[str, Any]:
        """The SDK MCP server config for ClaudeCodeOptions.mcp_servers."""
        from claude_code_sdk import create_sdk_mcp_server, tool

        sdk_tool = tool(
            PERPLEXITY_TOOL_NAME,
            # Same text as the cloud tool, with the CLI's name for the fetch tool.
            PERPLEXITY_TOOL["description"].replace("web_fetch", "WebFetch").replace("web_search", "WebSearch"),
            PERPLEXITY_TOOL["input_schema"],
        )(self.handle)
        config = dict(create_sdk_mcp_server(name=MCP_SERVER_NAME, tools=[sdk_tool]))
        # The CLI defers every MCP tool behind ToolSearch: the model sees only
        # the name and must load the schema first. Measured 02.10.2026 (Mühl,
        # Dev worker1): with POOL_PROMPT_SECTION in the prompt the model loaded
        # only WebSearch/WebFetch and called Perplexity 0 times. ``alwaysLoad``
        # on the server config (CLI >= 2.1.220, schema of type "sdk"; the SDK
        # passes every key except ``instance`` into --mcp-config) offers the
        # tool directly. Live probe CLI 2.1.220 + SDK 0.0.22: without the key
        # the model reports the tool as deferred, with it as directly callable.
        # A per-tool ``_meta["anthropic/alwaysLoad"]`` would not survive —
        # SDK 0.0.22 rebuilds tools/list from name/description/inputSchema only.
        config["alwaysLoad"] = True
        return config


def _cli_tool_names(text: str) -> str:
    """Cloud tool texts name the server tools; the CLI calls them differently."""
    return (
        text.replace("web_fetch", "WebFetch")
        .replace("web_search", "WebSearch")
        .replace(PERPLEXITY_TOOL_NAME, MCP_TOOL_NAME)
    )


class PoolDocumentTool:
    """fetch_document on the pool path: one instance per research run.

    Same shape as PoolPerplexityTool — the handler runs in this worker and
    raises PoolToolError for every refusal, so the CLI sees an error result
    that carries the named reason (src/research_cloud/fetch_document.py).
    """

    def __init__(self, max_uses: int, **fetch_kwargs: Any) -> None:
        self.counters = DocumentCounters(max_uses=max_uses)
        self._fetch_kwargs = fetch_kwargs

    async def handle(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            text = await fetch_document(args, self.counters, **self._fetch_kwargs)
        except DocumentFetchError as e:
            raise PoolToolError(f"fetch_document: {e}") from e
        return {"content": [{"type": "text", "text": text}]}

    def server(self) -> Dict[str, Any]:
        from claude_code_sdk import create_sdk_mcp_server, tool

        sdk_tool = tool(
            FETCH_DOCUMENT_TOOL_NAME,
            _cli_tool_names(FETCH_DOCUMENT_TOOL["description"]),
            FETCH_DOCUMENT_TOOL["input_schema"],
        )(self.handle)
        config = dict(create_sdk_mcp_server(name=DOC_MCP_SERVER_NAME, tools=[sdk_tool]))
        config["alwaysLoad"] = True  # same reason as PoolPerplexityTool.server()
        return config
