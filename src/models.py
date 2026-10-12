from typing import List, Optional, Dict, Any, Union, Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator, Discriminator
from enum import Enum
from datetime import datetime
import uuid

from config.logging_config import get_logger
from src.model_registry import get_default_model

logger = get_logger(__name__)


# ============================================================================
# Backend Types for Multi-Provider Support
# ============================================================================

class BackendType(str, Enum):
    """Backend provider for Claude API requests."""
    ANTHROPIC = "anthropic"            # Claude Code SDK (default, full tool support)
    BEDROCK = "bedrock"                # AWS Bedrock (DSGVO-compliant, EU data residency)
    OPENAI_COMPATIBLE = "openai_compatible"  # Generic OpenAI-compatible API (OpenRouter, etc.)
    GEMINI_CLI = "gemini_cli"          # Gemini CLI subprocess (Google OAuth, subscription models)
    ANTHROPIC_DIRECT = "anthropic_direct"  # Direct Anthropic Messages API (ANTHROPIC_VISION_API_KEY,
                                           # no CLI subprocess/tool support) — fallback-only tier for
                                           # requests that already run with enable_tools=false, see
                                           # src/providers/anthropic_direct.py
    GEMINI_API = "gemini_api"          # Google Gemini generateContent per API key, MIT Bildeingabe
                                       # (GEMINI_VISION_API_KEY). Auf der dev-Bridge der Standard fuer
                                       # die Bildanalyse, auf prod gesperrt — siehe
                                       # src/routing/gemini_vision_gate.py. Nicht zu verwechseln mit
                                       # GEMINI_CLI (OAuth-Subprozess, ohne Bildeingabe).


class PrivacyMode(str, Enum):
    """Privacy mode for PII anonymization."""
    AUTO = "auto"        # Bedrock EU → disabled, otherwise global setting
    ENABLED = "enabled"  # Always anonymize
    DISABLED = "disabled"  # Never anonymize


class CacheControl(BaseModel):
    """Cache-Grenze am Textblock (Anthropic-Form ``{"type": "ephemeral"}``).

    Unbekannte Schluessel (z. B. ``ttl``) werden ignoriert wie vor CACHE2, als dort
    das ganze ``cache_control`` still wegfiel — ein bisher laufender Aufruf darf
    nicht zu 422 werden (CACHE2R B2). Ignoriert wird aber LAUT (WARN mit den
    Schluesselnamen): wer ``ttl: 1h`` schickt, bekommt die 5-Minuten-Grenze der CLI,
    und eine Kostenmessung, die mit 1 h rechnet, waere sonst still falsch.
    """
    model_config = ConfigDict(extra="ignore")
    type: Literal["ephemeral"]

    @model_validator(mode="before")
    @classmethod
    def warn_unknown_keys(cls, data):
        if isinstance(data, dict):
            unknown = sorted(str(k) for k in data if k != "type")
            if unknown:
                logger.warning(
                    "cache_control: unbekannte Schluessel %s ignoriert — die Bridge reicht nur "
                    "{'type': 'ephemeral'} an die CLI durch (deren TTL gilt, 5 min)",
                    unknown,
                )
        return data


class TextContentPart(BaseModel):
    """Text content part for multimodal messages.

    ``cache_control`` markiert das Ende eines stabilen Praefixes. Die Bridge reicht
    die LETZTE Marke einer Anfrage als Blockgrenze bis zur CLI durch
    (src/cache_boundary.py); ohne Marke ist alles wie bisher.
    """
    type: Literal["text"]
    text: str
    cache_control: Optional[CacheControl] = None


class ImageUrlDetail(BaseModel):
    """Image URL detail object (OpenAI format)."""
    url: str
    detail: Optional[Literal["auto", "low", "high"]] = "auto"


class ImageUrlContentPart(BaseModel):
    """Image URL content part for multimodal messages (OpenAI format)."""
    type: Literal["image_url"]
    image_url: ImageUrlDetail


class ImageSourceDetail(BaseModel):
    """Image source detail object (Anthropic format)."""
    type: Literal["base64"]
    media_type: str
    data: str


class ImageContentPart(BaseModel):
    """Image content part for multimodal messages (Anthropic format)."""
    type: Literal["image"]
    source: ImageSourceDetail


# Union type for all content parts - supports text and images
# Using Discriminator for proper Pydantic v2 union parsing based on "type" field
ContentPart = Annotated[
    Union[TextContentPart, ImageUrlContentPart, ImageContentPart],
    Discriminator("type")
]


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: Union[str, List[ContentPart]]
    name: Optional[str] = None
    # Zeichenzahl des abgeflachten Inhalts bis einschliesslich des letzten Textblocks
    # mit cache_control; None ohne Marke. Privat: kein Aufrufer kann sie setzen.
    _cache_prefix_chars: Optional[int] = PrivateAttr(default=None)

    @property
    def cache_prefix_chars(self) -> Optional[int]:
        return self._cache_prefix_chars

    @model_validator(mode='after')
    def normalize_content(self):
        """Convert array content to string for Claude Code compatibility.

        IMPORTANT: If content contains images, do NOT normalize - VisionProvider handles it.
        Text-only: the string is unchanged by CACHE2; only the position of the last
        ``cache_control`` mark is remembered in ``cache_prefix_chars``.
        """
        if isinstance(self.content, list):
            # Check if content contains images - if so, DON'T normalize
            has_images = any(
                (isinstance(part, (ImageUrlContentPart, ImageContentPart))) or
                (isinstance(part, dict) and part.get("type") in ("image_url", "image"))
                for part in self.content
            )

            if has_images:
                # Keep as list for VisionProvider to handle
                return self

            # Text-only content: Extract text from content parts and concatenate
            text_parts = []
            marked_upto: Optional[int] = None
            for part in self.content:
                if isinstance(part, TextContentPart):
                    text_parts.append(part.text)
                    if part.cache_control is not None:
                        marked_upto = len(text_parts)
                elif isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))

            # Join all text parts with newlines
            self.content = "\n".join(text_parts) if text_parts else ""
            if marked_upto is not None:
                # Laenge von "\n".join(text_parts[:marked_upto]) ohne den String zu bauen
                prefix_len = sum(len(t) for t in text_parts[:marked_upto]) + (marked_upto - 1)
                self._cache_prefix_chars = prefix_len if prefix_len > 0 else None

        return self

    def has_images(self) -> bool:
        """Check if this message contains image content."""
        if isinstance(self.content, str):
            return False
        return any(
            isinstance(part, (ImageUrlContentPart, ImageContentPart)) or
            (isinstance(part, dict) and part.get("type") in ("image_url", "image"))
            for part in self.content
        )


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    temperature: Optional[float] = Field(default=1.0, ge=0, le=2)
    top_p: Optional[float] = Field(default=1.0, ge=0, le=1)
    n: Optional[int] = Field(default=1, ge=1)
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    max_tokens: Optional[int] = Field(
        default=64000,
        description="Maximum tokens in response. Defaults to 64000 (Claude 4.5 max) if not specified."
    )
    presence_penalty: Optional[float] = Field(default=0, ge=-2, le=2)
    frequency_penalty: Optional[float] = Field(default=0, ge=-2, le=2)
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
    session_id: Optional[str] = Field(default=None, description="Optional session ID for conversation continuity")
    enable_tools: Optional[bool] = Field(default=False, description="Enable Claude Code tools (Read, Write, Bash, etc.) - disabled by default for OpenAI compatibility")

    # Backend selection for multi-provider support
    backend: Optional[BackendType] = Field(
        default=BackendType.ANTHROPIC,
        description="Backend provider: 'anthropic' (default, Claude Code SDK) or 'bedrock' (AWS Bedrock EU for DSGVO compliance)"
    )
    bedrock_region: Optional[str] = Field(
        default=None,
        description="AWS region for Bedrock backend (default: eu-central-1). Only used when backend='bedrock'"
    )
    privacy: Optional[PrivacyMode] = Field(
        default=PrivacyMode.AUTO,
        description="Privacy mode: 'auto' (disable for Bedrock EU), 'enabled' (always anonymize), 'disabled' (never anonymize)"
    )
    provider_tier: Optional[str] = Field(
        default=None,
        description="Provider tier for multi-provider routing (e.g. 'claude-premium', 'dsgvo-deutschland', 'eu-standard'). Overrides backend selection."
    )

    # Gemini-EIGENE Denk-Einstellung (2026-09-03). Bewusst KEINE Uebersetzung
    # von Anthropics `thinking`/`output_config` — die bleiben auf dem
    # Gemini-Weg abgewiesen, weil eine Abbildung geraten waere. Dies hier ist
    # Geminis eigenes `thinkingConfig.thinkingBudget`, unter seinem eigenen
    # Namen durchgereicht.
    #
    # Warum pro Request und nicht nur als Deployment-Env: E3 misst "mit Denken"
    # gegen "ohne Denken". Als reine Env-Einstellung braeuchte das zwei Laeufe
    # mit Umstellung dazwischen — verschiedene Bridge-Last, verschiedener
    # Kontostand, verschiedene Bilder. Das waere kein Vergleich, sondern zwei
    # Messungen. Pro Request laufen alle Varianten im selben Fenster.
    #
    # 0 = Denken aus, positive Zahl = Obergrenze, None = Env-Default bzw.
    # Googles Verhalten. NUR zusammen mit dem Gemini-Bildweg zulaessig; sonst
    # 400 (siehe main.py) — ein Feld, das je nach Backend still nichts tut,
    # ist schlimmer als kein Feld.
    gemini_thinking_budget: Optional[int] = Field(
        default=None, ge=0,
        description=(
            "Gemini thinkingConfig.thinkingBudget: 0 disables thinking, a positive "
            "value caps it, omitted uses the Bridge/Google default. Only valid "
            "together with the Gemini vision path — rejected with 400 otherwise."
        ),
    )

    # Extended-thinking passthrough (2026-07-24). Forwarded VERBATIM to the raw
    # Anthropic-Messages-API-format backends only: Bedrock (bedrock_service.py)
    # and the direct-Anthropic fallback/vision path (vision_provider.py via
    # src/providers/anthropic_direct.py, src/routing/vision_router.py). Kept as
    # a raw dict rather than a strict submodel on purpose: Anthropic's thinking
    # schema is provider-owned and evolving (e.g. the "adaptive" type + sibling
    # `output_config.effort`), the Bridge is a proxy and must not re-implement
    # that validation — an unsupported shape/combination is surfaced as the
    # provider's own error (see call_bedrock's ClientError mapping and
    # VisionProvider.analyze's 4xx passthrough), never silently dropped.
    # None (the default) changes NOTHING: no key is added to the outgoing body,
    # so existing callers get byte-identical requests.
    thinking: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Passthrough for the Anthropic Messages API 'thinking' param, e.g. {'type': 'adaptive'}, {'type': 'enabled', 'budget_tokens': N}, or {'type': 'disabled'}. Forwarded verbatim on backend='bedrock' or a provider_tier that resolves to anthropic_direct. On the default Claude Code SDK backend, {'type': 'disabled'} and {'type': 'enabled', 'budget_tokens': N} set the CLI thinking budget (MAX_THINKING_TOKENS); other shapes are not applied there (see log_unsupported_parameters). Omit for unchanged default behavior."
    )
    output_config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Passthrough for the Anthropic Messages API 'output_config' param, e.g. {'effort': 'low'|'medium'|'high'|'max'}. Same forwarding scope and caveats as 'thinking'. Omit for unchanged default behavior."
    )

    @field_validator('messages')
    @classmethod
    def reject_cache_marker_chars(cls, v):
        # U+E000..U+E002 sind die Cache-Grenzmarke der Bridge (src/cache_boundary.py).
        # Steht eine davon schon im Text, waere die Grenze nicht eindeutig: abweisen —
        # aber NUR, wenn eine Grenze angefordert ist. Ohne cache_control laufen diese
        # Zeichen (Symbolschriften in extrahiertem PDF-Text) unveraendert durch wie
        # vor CACHE2 (CACHE2R B2).
        from src.cache_boundary import boundary_requested, contains_sentinel_chars
        if not boundary_requested(v):
            return v
        for m in v:
            texts = [m.content] if isinstance(m.content, str) else [
                getattr(p, "text", "") for p in m.content if getattr(p, "type", None) == "text"
            ]
            if any(contains_sentinel_chars(t or "") for t in texts):
                raise ValueError(
                    "message text contains reserved characters U+E000..U+E002 "
                    "(bridge cache-boundary marker); not allowed together with cache_control"
                )
        return v

    @field_validator('n')
    @classmethod
    def validate_n(cls, v):
        if v > 1:
            raise ValueError("Claude Code SDK does not support multiple choices (n > 1). Only single response generation is supported.")
        return v
    
    def log_unsupported_parameters(self):
        """Log warnings for parameters that are not supported by Claude Code SDK."""
        warnings = []
        
        if self.temperature != 1.0:
            warnings.append(f"temperature={self.temperature} is not supported by Claude Code SDK and will be ignored")
        
        if self.top_p != 1.0:
            warnings.append(f"top_p={self.top_p} is not supported by Claude Code SDK and will be ignored")
            
        # Note: max_tokens IS used for vision and Bedrock paths, only ignored by Claude Code SDK
        # No warning needed since we have a sensible default now
        
        if self.presence_penalty != 0:
            warnings.append(f"presence_penalty={self.presence_penalty} is not supported by Claude Code SDK and will be ignored")
            
        if self.frequency_penalty != 0:
            warnings.append(f"frequency_penalty={self.frequency_penalty} is not supported by Claude Code SDK and will be ignored")
            
        if self.logit_bias:
            warnings.append(f"logit_bias is not supported by Claude Code SDK and will be ignored")
            
        if self.stop:
            warnings.append(f"stop sequences are not supported by Claude Code SDK and will be ignored")

        # thinking {"type":"disabled"} / {"type":"enabled","budget_tokens":N} IS
        # applied on the SDK path (as the CLI's MAX_THINKING_TOKENS, see
        # sdk_max_thinking_tokens). Only shapes without a CLI equivalent
        # (e.g. "adaptive") stay unapplied — and say so.
        if self.thinking is not None and self.sdk_max_thinking_tokens() is None:
            warnings.append(f"thinking={self.thinking} has no Claude Code SDK equivalent (only {{'type':'disabled'}} and {{'type':'enabled','budget_tokens':N}} map to MAX_THINKING_TOKENS; everything else needs backend='bedrock' or an anthropic_direct provider_tier) and will be ignored on the SDK backend")

        if self.output_config is not None:
            warnings.append(f"output_config={self.output_config} is not supported by the Claude Code SDK backend (only backend='bedrock' or an anthropic_direct provider_tier forward it) and will be ignored")

        for warning in warnings:
            logger.warning(f"OpenAI API compatibility: {warning}")
    
    def sdk_max_thinking_tokens(self) -> Optional[int]:
        """Map the Anthropic `thinking` param onto the Claude Code CLI's thinking
        budget (env MAX_THINKING_TOKENS). The CLI thinks BY DEFAULT: without this,
        max_tokens is ignored on the SDK path and a one-word answer was measured
        with 7018 completion tokens (2026-09-24, report help-agent, 3.8 cent).
        Returns None when the shape has no CLI equivalent (CLI default applies)."""
        if not isinstance(self.thinking, dict):
            return None
        kind = self.thinking.get("type")
        if kind == "disabled":
            return 0
        if kind == "enabled":
            budget = self.thinking.get("budget_tokens")
            if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 0:
                return budget
        return None

    def to_claude_options(self) -> Dict[str, Any]:
        """Convert OpenAI request parameters to Claude Code SDK options."""
        # Log warnings for unsupported parameters
        self.log_unsupported_parameters()
        
        options = {}
        
        # Direct mappings
        if self.model:
            options['model'] = self.model

        # Thinking budget for the CLI subprocess. An X-Claude-Max-Thinking-Tokens
        # header (merged later by the caller) overrides this.
        _thinking_budget = self.sdk_max_thinking_tokens()
        if _thinking_budget is not None:
            options['max_thinking_tokens'] = _thinking_budget
            
        # Use user field for session identification if provided
        if self.user:
            # Could be used for analytics/logging or session tracking
            logger.info(f"Request from user: {self.user}")
        
        return options


class Choice(BaseModel):
    index: int
    message: Message
    finish_reason: Optional[str] = None


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    # Extended fields for AI usage tracking (added 2026-01-13)
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    image_count: Optional[int] = None  # Number of images analyzed (vision calls)


class BackendInfo(BaseModel):
    """Backend routing information (non-standard OpenAI extension)."""
    backend: str = Field(description="Backend used: 'anthropic' or 'bedrock'")
    region: Optional[str] = Field(default=None, description="AWS region (only for Bedrock)")
    privacy_applied: bool = Field(description="Whether PII anonymization was applied")
    model_id_used: str = Field(description="Actual model ID sent to backend")
    aws_request_id: Optional[str] = Field(
        default=None,
        description="AWS request ID (Bedrock only) — joins usage_events rows with AWS invocation logs for 1:1 billing reconciliation"
    )


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(datetime.now().timestamp()))
    model: str
    choices: List[Choice]
    usage: Optional[Usage] = None
    system_fingerprint: Optional[str] = None
    x_backend_info: Optional[BackendInfo] = Field(
        default=None,
        description="Backend routing information (non-standard OpenAI extension)"
    )
    x_bridge_finish: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "How the model ended (non-standard extension): stop_reason as the model "
            "reported it; on finish_reason content_filter also category and folgezuege"
        ),
    )
    # BRREF (12.10.2026): a CLI run can make several model calls (tool turns,
    # the CLI's own follow-up after a safety stop); usage is then their SUM.
    x_bridge_model_calls: Optional[int] = Field(
        default=None,
        description="Number of model calls usage is summed over (non-standard extension)",
    )
    x_bridge_usage_zuege: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Usage per model call; usage above is their sum (non-standard extension)",
    )


class StreamChoice(BaseModel):
    index: int
    delta: Dict[str, Any]
    finish_reason: Optional[str] = None


class ChatCompletionStreamResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(datetime.now().timestamp()))
    model: str
    choices: List[StreamChoice]
    system_fingerprint: Optional[str] = None
    # Final chunk only (BRREF): see ChatCompletionResponse.x_bridge_finish.
    x_bridge_finish: Optional[Dict[str, Any]] = None


class ErrorDetail(BaseModel):
    message: str
    type: str
    param: Optional[str] = None
    code: Optional[str] = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class SessionInfo(BaseModel):
    session_id: str
    created_at: datetime
    last_accessed: datetime
    message_count: int
    expires_at: datetime


class SessionListResponse(BaseModel):
    sessions: List[SessionInfo]
    total: int


# ============================================================================
# Research Endpoint Models
# ============================================================================

class ResearchRequest(BaseModel):
    """
    Request model for dedicated /v1/research endpoint.

    Designed for Claude Code research tasks with SuperClaude integration.
    Supports depth levels, planning strategies, and advanced research options.
    """
    # Core Research Parameters
    query: str = Field(..., description="Research question or topic to investigate")

    # OA-Scholarly-Schicht (Weg A): "academic" aktiviert Pre-Retrieval echter Open-Access-Volltexte
    # (OpenAlex/Unpaywall) VOR dem Agent-Run. Nur wirksam, wenn zusätzlich BRIDGE_SCHOLARLY_ENABLED=true.
    # Default "standard" → keine Verhaltensänderung für bestehende Caller.
    research_mode: Optional[Literal["standard", "academic"]] = Field(
        default="standard",
        description="standard = wie bisher (offene Web-Suche); academic = zusätzlich legale OA-Volltext-Injektion"
    )

    # Model Configuration
    model: Optional[str] = Field(
        default_factory=lambda: get_default_model("sonnet").id,
        description="Claude model to use for research (default: Sonnet-Default aus model_registry SSoT)"
    )

    # Output Configuration
    output_path: Optional[str] = Field(
        default=None,
        description="Host filesystem path where research report should be saved. If not provided, saves to /tmp/"
    )

    # SuperClaude Research Depth
    depth: Optional[Literal["quick", "standard", "deep", "exhaustive"]] = Field(
        default="standard",
        description="Research depth: quick (1-2min, 1 hop), standard (3-5min, 2-3 hops), deep (5-8min, 3-4 hops), exhaustive (8-15min, 5 hops)"
    )

    # SuperClaude Planning Strategy
    strategy: Optional[Literal["planning", "intent", "unified"]] = Field(
        default="unified",
        description="Planning strategy: planning (immediate execution), intent (clarification first), unified (collaborative planning)"
    )

    # Advanced Research Options
    max_hops: Optional[int] = Field(
        default=None,
        ge=1,
        le=5,
        description="Maximum research hops (overrides depth setting). 1-5 hops for multi-hop exploration"
    )

    confidence_threshold: Optional[float] = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Minimum confidence score for research quality (0.0-1.0)"
    )

    parallel_searches: Optional[int] = Field(
        default=5,
        ge=1,
        le=5,
        description="Maximum concurrent searches (1-5). Higher = faster but more resource intensive"
    )

    source_filter: Optional[List[Literal["tier_1", "tier_2", "tier_3", "tier_4"]]] = Field(
        default=None,
        description="Filter sources by credibility tier. tier_1=academic/official, tier_2=established media, tier_3=community, tier_4=forums"
    )

    # Claude Code SDK Options
    max_tokens: Optional[int] = Field(
        default=4000,
        description="Maximum tokens for response generation"
    )

    temperature: Optional[float] = Field(
        default=None,
        ge=0,
        le=2,
        description="Temperature for response generation (currently not supported by Claude Code SDK)"
    )

    max_turns: Optional[int] = Field(
        default=30,
        description="Maximum conversation turns for research task (increased for deep research)"
    )

    # Backend selection for multi-provider support
    backend: Optional[BackendType] = Field(
        default=BackendType.ANTHROPIC,
        description="Backend provider: 'anthropic' (default) or 'bedrock' (AWS Bedrock EU for DSGVO)"
    )
    bedrock_region: Optional[str] = Field(
        default=None,
        description="AWS region for Bedrock backend (default: eu-central-1)"
    )
    privacy: Optional[PrivacyMode] = Field(
        default=PrivacyMode.AUTO,
        description="Privacy mode: 'auto', 'enabled', or 'disabled'"
    )
    # Mirrors ChatCompletionRequest.provider_tier so the shared provider-routing
    # path (user_provider_override, app_tier_policy, backend_router) applies to
    # /v1/research identically to /v1/chat/completions — the research handler
    # explicitly enforces "the same provider pins as chat completions", but the
    # model was missing this one field, so a compliance-pin write
    # (request_body.provider_tier = None) raised "ResearchRequest object has no
    # field provider_tier" and 500'd the research self-call.
    provider_tier: Optional[str] = Field(
        default=None,
        description="Provider tier for multi-provider routing (e.g. 'claude-premium', 'eu-standard'). Overrides backend selection."
    )

    # Async mode (for clients that cannot keep an HTTP connection open for 5-15min,
    # e.g. Vercel Serverless Functions with 300s hard limit). When true, the endpoint
    # returns immediately with a request_id; poll GET /v1/research/async/{request_id}
    # until status='done'.
    async_mode: Optional[bool] = Field(
        default=False,
        description="If true, returns immediately with request_id. Poll /v1/research/async/{request_id} for result."
    )

    # Research-cloud overflow opt-in (Weg C, see src/research_cloud/). Default
    # false -> existing callers unaffected. Only takes effect when the pool
    # looks saturated AND RESEARCH_CLOUD_ENABLED is on (src.research_cloud.
    # routing.resolve_research_cloud_routing) — this flag alone never routes
    # a request off the subscription pool.
    cloud_overflow: Optional[bool] = Field(
        default=False,
        description="Allow this research call to overflow to the Anthropic-API research-cloud path if the subscription worker pool looks saturated. No effect unless the research-cloud feature is enabled server-side."
    )

    # Output contract (BRRES, 11.10.2026). Default false = unchanged: the
    # answer is the report FILE the run writes (BR2). True = the answer is the
    # text of the run's FINAL assistant message — for callers that parse a
    # prescribed format out of the reply (Energy: ```pruefwissen-auswahl```).
    # Under the file contract such a caller is overruled by the bridge's own
    # "write the report into OUTPUT_FILE_PATH" order, and got back whatever
    # file the run touched first (a Python script, a 926-char summary).
    antwort_im_text: Optional[bool] = Field(
        default=False,
        description=(
            "Output contract. true: the answer is the text of the run's final assistant "
            "message, in the format the query prescribes; the run gets no file to write "
            "and no Write/Edit/Bash tools, files are never returned. false (default): the "
            "answer is the report file the run writes. Response field antwort_quelle "
            "says which one was used."
        ),
    )

    # Gap check per request (P6NOGAP, 12.10.2026). Default None = unchanged:
    # the deployment flag RESEARCH_LUECKEN_NACHKONTROLLE decides. False = this
    # request runs without the gap checker and without the return round — for
    # callers that check the report themselves afterwards (Energy phase 6: its
    # applicability check). A request can only switch it off, never on: the
    # return round needs Perplexity, which only the deployment can provide.
    luecken_nachkontrolle: Optional[bool] = Field(
        default=None,
        description=(
            "Gap check for this request. false: no gap checker and no return round; the "
            "response field nachkontrolle says status=uebersprungen with the reason. "
            "null/true (default): the deployment setting decides."
        ),
    )


class ErrorVerdictFields(BaseModel):
    """retryable / retry_after_s on endpoints that answer errors with HTTP 200
    and status="error" (research, doc-agent). Rules: src/error_contract.py.
    An error nobody marked transient is final (retryable=False), never absent,
    so callers can read the field without guessing."""
    retryable: Optional[bool] = Field(
        default=None,
        description="status='error' only: True = the same request can succeed later unchanged",
    )
    retry_after_s: Optional[int] = Field(
        default=None,
        description="status='error' only: seconds to wait, when the bridge knows",
    )

    @model_validator(mode="after")
    def _error_has_verdict(self):
        if getattr(self, "status", None) == "error" and self.retryable is None:
            from src.error_contract import UNCLASSIFIED_RETRYABLE
            self.retryable = UNCLASSIFIED_RETRYABLE
        return self


class ResearchUsage(Usage):
    """Token usage of one research run (RUSAGE, 11.10.2026).

    The chat/completions shape (Usage: prompt_tokens = uncached + cache-write +
    cache-read input, completion_tokens, cache_read_tokens, cache_write_tokens)
    plus the Anthropic names the CLI and the Messages API report
    (input_tokens = UNCACHED input only). Both from the same numbers: a caller
    that reads chat usage reads this, one that reads Anthropic usage too.
    Sum over every round the run paid for (repair round, gap round)."""
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int

    @classmethod
    def aus_summen(cls, input_tokens: int, output_tokens: int,
                   cache_creation_input_tokens: int,
                   cache_read_input_tokens: int) -> "ResearchUsage":
        prompt = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
        return cls(
            prompt_tokens=prompt,
            completion_tokens=output_tokens,
            total_tokens=prompt + output_tokens,
            cache_read_tokens=cache_read_input_tokens,
            cache_write_tokens=cache_creation_input_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )


# usage_fehlt when the path that built the response measured nothing — e.g. a
# request refused before any run. The run paths name their own reason.
RESEARCH_USAGE_NICHT_GEMESSEN = "nicht_gemessen"


class ResearchResponse(ErrorVerdictFields):
    """
    Response model for research endpoint.

    Returns both container and host file paths, plus execution metadata.
    """
    status: Literal["success", "error"]
    query: str
    model: str
    output_file: Optional[str] = Field(
        default=None,
        description="Host filesystem path where research report was saved"
    )
    container_file: Optional[str] = Field(
        default=None,
        description="Container filesystem path where research was generated"
    )
    execution_time_seconds: Optional[float] = Field(
        default=None,
        description="Time taken to complete research in seconds"
    )
    file_size_bytes: Optional[int] = Field(
        default=None,
        description="Size of generated research report in bytes"
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message if status is 'error'"
    )
    content: Optional[str] = Field(
        default=None,
        description="Actual research output content (markdown)"
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Claude Code session ID used for research"
    )
    request_id: Optional[str] = Field(
        default=None,
        description="Async job ID (only set when async_mode=true). Use GET /v1/research/async/{request_id} to poll."
    )
    # Beobachtbarkeit pro Lauf: macht Tool-Nutzung pruefbar (Eval/Abnahme),
    # statt sie aus dem Report-Text erraten zu muessen.
    #
    # web_searches/web_fetches zaehlt nur der Cloud-Executor — auf dem
    # Worker-Pool-Pfad fuehrt die CLI die Suche selbst und meldet keine
    # Aufrufzahl zurueck, dort bleiben sie None.
    # library_calls fuellen BEIDE Wege (seit 07.09.2026, Karte k20): der Cloud-
    # Weg zaehlt library_index/library_get, der Pool-Weg die Read/Grep/Glob-
    # Zugriffe auf den Bibliotheksordner im Arbeitsverzeichnis. None heisst
    # "Bibliothek war fuer diesen Lauf nicht eingeschaltet" und ist damit etwas
    # anderes als 0 ("stand bereit, wurde nicht gebraucht").
    web_searches: Optional[int] = Field(
        default=None,
        description="research-cloud only: number of web_search tool calls in this run"
    )
    web_fetches: Optional[int] = Field(
        default=None,
        description="research-cloud only: number of web_fetch tool calls in this run"
    )
    library_calls: Optional[int] = Field(
        default=None,
        description=(
            "number of curated-library accesses in this run — library_index/library_get "
            "on the research-cloud path, file reads under the library workdir on the "
            "worker-pool path; None when the library was off"
        )
    )
    antwort_quelle: Optional[Literal["datei", "text"]] = Field(
        default=None,
        description=(
            "success only: where content came from — 'datei' = the report file the run "
            "wrote (path in container_file), 'text' = the final assistant message "
            "(antwort_im_text=true, or the research-cloud path). None on errors and "
            "on bridges that predate the field."
        ),
    )
    antwort_ende: Optional[str] = Field(
        default=None,
        description=(
            "worker-pool and research-cloud path, success AND error: how the run's final "
            "assistant message ended — the CLI's stop_reason ('end_turn', 'tool_use', ...), "
            "'max_tokens' = cut at the output limit, 'max_tokens_fortgesetzt' = cut there "
            "and continued automatically by the CLI, so the final message is only the tail "
            "of the answer. None when the run reported no end."
        ),
    )
    fehler_code: Optional[Literal[
        "antwort_abgeschnitten", "modell_abbruch", "privacy_gate_failed", "privacy_service_unreachable",
        "laufzeit_erschoepft",
    ]] = Field(
        default=None,
        description=(
            "status='error' only, when the bridge knows the cause: 'antwort_abgeschnitten' = "
            "antwort_im_text and the final message ended at the output limit "
            "(antwort_ende 'max_tokens'/'max_tokens_fortgesetzt'). Not 'no answer', not "
            "error_max_turns: the same request will hit the same limit (retryable=false), "
            "a smaller one will not. 'modell_abbruch' = the model's safety classifier "
            "stopped a message of the run (antwort_ende 'refusal'), for either answer "
            "contract (retryable=false). 'privacy_gate_failed' = the anonymize gate "
            "(research-cloud prompt or perplexity_search) did not attest a verified "
            "anonymization — nothing was sent (retryable=false). 'privacy_service_unreachable' "
            "= no privacy endpoint answered (primary and fallback) — retryable once the service "
            "is back. 'laufzeit_erschoepft' = a mandatory round (main run, repair "
            "round) did not finish within the run's overall deadline (laufzeit.frist_s) or "
            "the CLI's MAX_TIMEOUT; the agent was terminated, no partial report "
            "(retryable=false). None for every other error."
        ),
    )
    nachkontrolle: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Pool path with the gap check on, success AND error: what the check after the "
            "report did. status 'durchgefuehrt' (checked; nothing open, or the return round "
            "revised the report), 'uebersprungen' (a step left out — the run's remaining time "
            "or the perplexity budget; grund names it), 'gescheitert' (the check or the "
            "return round failed, e.g. it ran out of time — the report handed out is the "
            "complete one from BEFORE the return round). Also: rueckrunde, luecken_gemeldet, "
            "luecken_ohne_suche, luecken_ohne_suche_nach_rueckrunde, nachmessung. None when "
            "the gap check was not part of the run."
        ),
    )
    laufzeit: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Pool path, success AND error: gesamt_s = run time of the whole call (every "
            "round), frist_s = the run's overall deadline, runden = seconds per round "
            "(hauptlauf, reparatur, pruefer, rueckrunde, nachmessung). execution_time_seconds "
            "is the same whole-call time."
        ),
    )
    usage: Optional[ResearchUsage] = Field(
        default=None,
        description=(
            "success AND error: tokens the run used, as far as the run reported them "
            "(the CLI's result message, the Messages API on the research-cloud path). "
            "None when nothing was reported — never a made-up 0; usage_fehlt says why."
        ),
    )
    usage_fehlt: Optional[str] = Field(
        default=None,
        description=(
            "set exactly when usage is None: why there is no usage, e.g. "
            "'cli_ohne_usage' (the run ended without a result carrying usage), "
            "'research_cloud_fehler' (the cloud run failed before reporting it), "
            "'nicht_gemessen' (no run measured, e.g. refused before it started)."
        ),
    )

    usage_unvollstaendig: Optional[str] = Field(
        default=None,
        description=(
            "set when usage is present but does not cover every round of the run: "
            "e.g. 'research_cloud_ohne_usage: 1 von 3 Runden ohne usage' — the "
            "usage sums are a lower bound then. None when complete or usage is None."
        ),
    )

    @model_validator(mode="after")
    def _usage_oder_grund(self):
        if self.usage is not None:
            self.usage_fehlt = None
        else:
            self.usage_unvollstaendig = None
        if self.usage is None and not self.usage_fehlt:
            self.usage_fehlt = RESEARCH_USAGE_NICHT_GEMESSEN
        return self


class DocAgentFile(BaseModel):
    """One seeded document for the doc-agent workdir."""
    name: str = Field(..., description="Filename (basename only — sanitized server-side)")
    content: str = Field(..., description="Markdown/text content of the document")


class DocAgentRequest(BaseModel):
    """
    Request model for /v1/doc-agent — a Claude Code agent that answers a
    question over a seeded document workdir (Read/Grep/Glob only, no web).

    Unlike /v1/research this never touches the internet: the agent navigates
    the provided files itself, so callers do not need summary-based
    pre-selection and the answer scales with any number of documents.
    """
    question: str = Field(..., description="User question to answer from the documents")
    files: List[DocAgentFile] = Field(..., description="Documents seeded into the agent workdir")
    model: Optional[str] = Field(
        default_factory=lambda: get_default_model("sonnet").id,
        description="Claude model to use (default: Sonnet-Default aus model_registry SSoT)"
    )
    max_turns: Optional[int] = Field(
        default=25,
        ge=1,
        le=60,
        description="Maximum agent turns for navigating the documents"
    )


class DocAgentResponse(ErrorVerdictFields):
    """Response model for /v1/doc-agent."""
    status: Literal["success", "error"]
    question: str
    model: str
    answer: Optional[str] = Field(default=None, description="Final answer (markdown)")
    files_read: List[str] = Field(
        default_factory=list,
        description="Workdir-relative paths the agent actually read/grepped"
    )
    execution_time_seconds: Optional[float] = None
    session_id: Optional[str] = None
    error: Optional[str] = None


class AsyncResearchStatus(BaseModel):
    """Status response for async research polling."""
    status: Literal["running", "done", "error", "not_found"]
    request_id: str
    elapsed_seconds: Optional[float] = None
    result: Optional[ResearchResponse] = None
    error: Optional[str] = None
    # Machine-readable failure class. Currently only "orphaned": the worker
    # holding the job died (restart/redeploy) — the JOB was never at fault, so
    # callers may safely re-dispatch. A plain research failure has no kind.
    error_kind: Optional[Literal["orphaned"]] = None


# ============================================================================
# Smart Anonymization Endpoint Models
# ============================================================================

class SmartAnonymizeRequest(BaseModel):
    """
    Request model for /v1/privacy/smart-anonymize endpoint.

    3-stage pseudonymization:
    1. Presidio detects all PII aggressively
    2. Claude Haiku evaluates which entities are safe to restore
    3. Client-side encryption of mapping (handled in frontend)
    """
    text: str = Field(..., description="Text to smart-anonymize")
    language: Optional[str] = Field(
        default="de",
        description="Language for PII detection: 'de' or 'en'"
    )
    context_hint: Optional[str] = Field(
        default=None,
        description="Document context hint (e.g. 'Baugutachten', 'Energieausweis', 'Schallschutzgutachten')"
    )
    prefix: Optional[str] = Field(
        default=None,
        description="Document-scoped prefix for placeholders (e.g. 'Da1b2c3'). Default: 'ANON'"
    )
    known_entities: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Optional: Platzhalter -> Original aus frueheren Dokumenten desselben Akts. "
            "Gleiche Werte (und eindeutige Schreibvarianten) bekommen denselben Platzhalter "
            "und werden auch dort maskiert, wo der Detektor sie in diesem Dokument nicht fand. "
            "Wirkt nur mit BRIDGE_PSEUDONYM_POSTPROCESS=true."
        ),
    )


class SmartAnonymizeResponse(BaseModel):
    """
    Response model for smart anonymization.

    Returns both raw Presidio results and AI-refined results for comparison.
    """
    status: Literal["success", "error"]

    # Positive attestation that the real PII detector executed (Presidio ran).
    # Distinguishes a genuine clean-input pass (0 entities, detector ran) from a
    # disabled no-op that echoes the input back — consumers fail loud on a
    # "success" that lacks this. The disabled branch never sets it (it now 503s).
    anonymization_performed: bool = Field(
        default=False,
        description="True iff the Presidio/AI detector actually ran on this request"
    )

    # Stage 1: Raw Presidio result
    raw_anonymized_text: Optional[str] = Field(
        default=None,
        description="Presidio-anonymized text (all entities replaced)"
    )
    raw_entity_count: Optional[int] = Field(
        default=None,
        description="Number of entities detected by Presidio"
    )

    # Smart result (identical to raw since the AI-refinement stage was
    # removed 2026-07-03; fields kept for response-shape compatibility)
    smart_anonymized_text: Optional[str] = Field(
        default=None,
        description="Smart-anonymized text (only real PII replaced)"
    )
    smart_entity_count: Optional[int] = Field(
        default=None,
        description="Number of entities that remain anonymized"
    )
    restored_entities: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Always empty since the AI-refinement stage was removed (2026-07-03)"
    )

    # Mapping for client-side encryption
    mapping: Optional[Dict[str, str]] = Field(
        default=None,
        description="Reduced mapping (only kept entities): placeholder → original"
    )
    detected_entities: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="All detected entities with AI decisions"
    )
    postprocessing: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Wertfreie Statistik der Worker-Nachbearbeitung (src/privacy_postprocess), sonst leer"
    )

    # Meta
    error: Optional[str] = Field(
        default=None,
        description="Error message if status is 'error'"
    )


# ============================================================================
# PDF Conversion Endpoint Models (Docling)
# ============================================================================

class ConvertPdfResponse(BaseModel):
    """
    Response model for /v1/convert-pdf endpoint.

    Returns Markdown content + extracted images as base64.
    Docling runs locally on Bridge (Hetzner) — no external data transfer.
    """
    status: Literal["success", "error"]

    # Conversion result
    markdown: Optional[str] = Field(
        default=None,
        description="Converted Markdown content with image references"
    )
    images: Optional[Dict[str, str]] = Field(
        default=None,
        description="Extracted images as {filename: base64_data} dict"
    )
    image_count: Optional[int] = Field(
        default=None,
        description="Number of extracted images"
    )

    # Metadata
    pages: Optional[int] = Field(
        default=None,
        description="Number of pages in source PDF"
    )
    original_size_bytes: Optional[int] = Field(
        default=None,
        description="Size of original PDF in bytes"
    )
    markdown_size_bytes: Optional[int] = Field(
        default=None,
        description="Size of generated Markdown in bytes"
    )
    conversion_time_seconds: Optional[float] = Field(
        default=None,
        description="Time taken for conversion in seconds"
    )

    # Error
    error: Optional[str] = Field(
        default=None,
        description="Error message if status is 'error'"
    )