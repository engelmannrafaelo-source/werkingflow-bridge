from typing import List, Optional, Dict, Any, Union, Literal, Annotated
from pydantic import BaseModel, Field, field_validator, model_validator, Discriminator
from enum import Enum
from datetime import datetime
import uuid

from config.logging_config import get_logger

logger = get_logger(__name__)


# ============================================================================
# Backend Types for Multi-Provider Support
# ============================================================================

class BackendType(str, Enum):
    """Backend provider for Claude API requests."""
    ANTHROPIC = "anthropic"  # Claude Code SDK (default, full tool support)
    BEDROCK = "bedrock"      # AWS Bedrock (DSGVO-compliant, EU data residency)


class PrivacyMode(str, Enum):
    """Privacy mode for PII anonymization."""
    AUTO = "auto"        # Bedrock EU → disabled, otherwise global setting
    ENABLED = "enabled"  # Always anonymize
    DISABLED = "disabled"  # Never anonymize


class TextContentPart(BaseModel):
    """Text content part for multimodal messages."""
    type: Literal["text"]
    text: str


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

    @model_validator(mode='after')
    def normalize_content(self):
        """Convert array content to string for Claude Code compatibility.

        IMPORTANT: If content contains images, do NOT normalize - VisionProvider handles it.
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
            for part in self.content:
                if isinstance(part, TextContentPart):
                    text_parts.append(part.text)
                elif isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))

            # Join all text parts with newlines
            self.content = "\n".join(text_parts) if text_parts else ""

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
        
        for warning in warnings:
            logger.warning(f"OpenAI API compatibility: {warning}")
    
    def to_claude_options(self) -> Dict[str, Any]:
        """Convert OpenAI request parameters to Claude Code SDK options."""
        # Log warnings for unsupported parameters
        self.log_unsupported_parameters()
        
        options = {}
        
        # Direct mappings
        if self.model:
            options['model'] = self.model
            
        # Use user field for session identification if provided
        if self.user:
            # Could be used for analytics/logging or session tracking
            logger.info(f"Request from user: {self.user}")
        
        return options


class Choice(BaseModel):
    index: int
    message: Message
    finish_reason: Optional[Literal["stop", "length", "content_filter", "null"]] = None


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


class StreamChoice(BaseModel):
    index: int
    delta: Dict[str, Any]
    finish_reason: Optional[Literal["stop", "length", "content_filter", "null"]] = None


class ChatCompletionStreamResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(datetime.now().timestamp()))
    model: str
    choices: List[StreamChoice]
    system_fingerprint: Optional[str] = None


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

    # Model Configuration
    model: Optional[str] = Field(
        default="claude-sonnet-4-5-20250929",
        description="Claude model to use for research"
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


class ResearchResponse(BaseModel):
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