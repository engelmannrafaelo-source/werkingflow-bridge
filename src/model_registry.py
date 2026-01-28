"""
Model Registry - Zentrale Modell-Verwaltung mit Fuzzy Matching

Stellt sicher dass:
1. Alle Modelle an einer Stelle definiert sind
2. Fuzzy Matching funktioniert: "sonnet" → aktuellstes Sonnet, "haiku" → aktuellstes Haiku
3. Saubere Fehlermeldungen bei unbekannten Modellen
"""

from typing import Optional, List, Dict
from dataclasses import dataclass
from datetime import date
from config.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ModelInfo:
    """Information about a Claude model."""
    id: str
    family: str  # "sonnet", "haiku", "opus"
    version: str  # "4.5", "4", "3.7", "3.5", "3"
    release_date: date
    description: str
    is_default: bool = False  # True if this is the default for its family


# =============================================================================
# ZENTRALE MODELL-DEFINITION - EINZIGE QUELLE DER WAHRHEIT
# =============================================================================

MODELS: List[ModelInfo] = [
    # Sonnet Familie
    ModelInfo(
        id="claude-sonnet-4-5-20250929",
        family="sonnet",
        version="4.5",
        release_date=date(2025, 9, 29),
        description="Sonnet 4.5 - Neuestes und schnellstes Sonnet",
        is_default=True
    ),
    ModelInfo(
        id="claude-sonnet-4-20250514",
        family="sonnet",
        version="4",
        release_date=date(2025, 5, 14),
        description="Sonnet 4 - May 2025 Release"
    ),
    ModelInfo(
        id="claude-3-7-sonnet-20250219",
        family="sonnet",
        version="3.7",
        release_date=date(2025, 2, 19),
        description="Sonnet 3.7 - Extended thinking"
    ),
    ModelInfo(
        id="claude-3-5-sonnet-20241022",
        family="sonnet",
        version="3.5",
        release_date=date(2024, 10, 22),
        description="Sonnet 3.5 v2 - Legacy"
    ),

    # Haiku Familie
    ModelInfo(
        id="claude-haiku-4-5-20251001",
        family="haiku",
        version="4.5",
        release_date=date(2025, 10, 1),
        description="Haiku 4.5 - Neuestes und schnellstes Haiku, ideal fuer Vision",
        is_default=True
    ),
    ModelInfo(
        id="claude-3-5-haiku-20241022",
        family="haiku",
        version="3.5",
        release_date=date(2024, 10, 22),
        description="Haiku 3.5 - Legacy"
    ),

    # Opus Familie
    ModelInfo(
        id="claude-opus-4-20250514",
        family="opus",
        version="4",
        release_date=date(2025, 5, 14),
        description="Opus 4 - Leistungsfaehigstes Modell",
        is_default=True
    ),
    ModelInfo(
        id="claude-opus-4-1-20250805",
        family="opus",
        version="4.1",
        release_date=date(2025, 8, 5),
        description="Opus 4.1 - August 2025 Release"
    ),
]

# Build lookup dictionaries
_MODEL_BY_ID: Dict[str, ModelInfo] = {m.id: m for m in MODELS}
_DEFAULT_BY_FAMILY: Dict[str, ModelInfo] = {m.family: m for m in MODELS if m.is_default}


def get_all_model_ids() -> List[str]:
    """Get list of all supported model IDs."""
    return list(_MODEL_BY_ID.keys())


def get_model_info(model_id: str) -> Optional[ModelInfo]:
    """Get model info by exact ID."""
    return _MODEL_BY_ID.get(model_id)


def is_model_supported(model_id: str) -> bool:
    """Check if a model ID is supported."""
    return model_id in _MODEL_BY_ID


def get_default_model(family: str = "sonnet") -> Optional[ModelInfo]:
    """Get the default (newest) model for a family."""
    return _DEFAULT_BY_FAMILY.get(family.lower())


def resolve_model(model_input: str) -> tuple[str, Optional[str]]:
    """
    Resolve a model input to an actual model ID with fuzzy matching.

    Args:
        model_input: Model ID or fuzzy name (e.g., "sonnet", "haiku", "opus")

    Returns:
        Tuple of (resolved_model_id, warning_message)
        - If exact match: (model_id, None)
        - If fuzzy match: (resolved_model_id, "Resolved 'sonnet' to 'claude-sonnet-4-5-20250929'")
        - If no match: (None, "Model 'xyz' not supported. Available: ...")

    Examples:
        >>> resolve_model("claude-sonnet-4-5-20250929")
        ("claude-sonnet-4-5-20250929", None)

        >>> resolve_model("sonnet")
        ("claude-sonnet-4-5-20250929", "Resolved 'sonnet' to 'claude-sonnet-4-5-20250929' (Sonnet 4.5)")

        >>> resolve_model("haiku")
        ("claude-haiku-4-5-20251001", "Resolved 'haiku' to 'claude-haiku-4-5-20251001' (Haiku 4.5)")

        >>> resolve_model("xyz-unknown")
        (None, "Model 'xyz-unknown' not supported. Available models: sonnet, haiku, opus or full IDs")
    """
    model_lower = model_input.lower().strip()

    # 1. Exact match
    if model_input in _MODEL_BY_ID:
        logger.debug(f"Model exact match: {model_input}")
        return (model_input, None)

    # 2. Case-insensitive exact match
    for model_id in _MODEL_BY_ID:
        if model_id.lower() == model_lower:
            logger.info(f"Model case-insensitive match: {model_input} -> {model_id}")
            return (model_id, f"Resolved '{model_input}' to '{model_id}' (case corrected)")

    # 3. Fuzzy match by family name
    family_keywords = {
        "sonnet": ["sonnet", "son", "sonne"],
        "haiku": ["haiku", "hai", "haku", "heiko"],  # Include common typos
        "opus": ["opus", "op"],
    }

    for family, keywords in family_keywords.items():
        for keyword in keywords:
            if keyword in model_lower or model_lower in keyword:
                default_model = _DEFAULT_BY_FAMILY.get(family)
                if default_model:
                    msg = f"Resolved '{model_input}' to '{default_model.id}' ({default_model.description})"
                    logger.info(msg)
                    return (default_model.id, msg)

    # 4. Partial match on model ID
    for model_id, model_info in _MODEL_BY_ID.items():
        if model_lower in model_id.lower():
            msg = f"Resolved '{model_input}' to '{model_id}' (partial match)"
            logger.info(msg)
            return (model_id, msg)

    # 5. No match - return error
    available = ", ".join(get_all_model_ids())
    error_msg = (
        f"Model '{model_input}' not supported. "
        f"Use one of: sonnet, haiku, opus (for latest) or exact IDs: {available}"
    )
    logger.warning(error_msg)
    return (None, error_msg)


def get_models_for_api() -> List[Dict]:
    """Get model list in OpenAI API format for /v1/models endpoint."""
    return [
        {
            "id": model.id,
            "object": "model",
            "owned_by": "anthropic",
            "description": model.description
        }
        for model in MODELS
    ]


class ModelResolutionError(Exception):
    """Raised when a model cannot be resolved."""
    def __init__(self, model_input: str, available_models: List[str]):
        self.model_input = model_input
        self.available_models = available_models
        super().__init__(
            f"Model '{model_input}' not supported. "
            f"Available: sonnet, haiku, opus (for latest) or: {', '.join(available_models)}"
        )


def resolve_model_strict(model_input: str) -> str:
    """
    Resolve model with strict error handling.

    Raises:
        ModelResolutionError: If model cannot be resolved

    Returns:
        Resolved model ID
    """
    resolved_id, warning = resolve_model(model_input)

    if resolved_id is None:
        raise ModelResolutionError(model_input, get_all_model_ids())

    if warning:
        logger.info(warning)

    return resolved_id


# =============================================================================
# AWS BEDROCK MODEL ID CONVERSION
# =============================================================================

def to_bedrock_model_id(anthropic_model_id: str) -> str:
    """Convert Anthropic model ID to AWS Bedrock model ID.

    Bedrock uses format: anthropic.{model-name}-v{version}:{revision}

    Args:
        anthropic_model_id: e.g., 'claude-sonnet-4-5-20250929'

    Returns:
        Bedrock model ID: e.g., 'anthropic.claude-sonnet-4-5-20250929-v1:0'

    Raises:
        ValueError: If model ID format is not recognized

    Examples:
        >>> to_bedrock_model_id("claude-sonnet-4-5-20250929")
        "anthropic.claude-sonnet-4-5-20250929-v1:0"

        >>> to_bedrock_model_id("claude-haiku-4-5-20251001")
        "anthropic.claude-haiku-4-5-20251001-v1:0"
    """
    if not anthropic_model_id.startswith("claude-"):
        raise ValueError(
            f"Cannot convert non-Claude model '{anthropic_model_id}' to Bedrock ID. "
            f"Model must start with 'claude-'"
        )

    # Bedrock format: anthropic.{model-id}-v1:0
    return f"anthropic.{anthropic_model_id}-v1:0"


def from_bedrock_model_id(bedrock_model_id: str) -> str:
    """Convert AWS Bedrock model ID back to Anthropic model ID.

    Args:
        bedrock_model_id: e.g., 'anthropic.claude-sonnet-4-5-20250929-v1:0'

    Returns:
        Anthropic model ID: e.g., 'claude-sonnet-4-5-20250929'

    Raises:
        ValueError: If Bedrock model ID format is invalid

    Examples:
        >>> from_bedrock_model_id("anthropic.claude-sonnet-4-5-20250929-v1:0")
        "claude-sonnet-4-5-20250929"
    """
    if not bedrock_model_id.startswith("anthropic."):
        raise ValueError(
            f"Invalid Bedrock model ID format: '{bedrock_model_id}'. "
            f"Expected format: anthropic.claude-*-v1:0"
        )

    # Remove 'anthropic.' prefix
    model_part = bedrock_model_id[10:]

    # Remove version suffix (-v1:0, -v1:1, -v2:0, etc.)
    if "-v1:0" in model_part:
        model_part = model_part.rsplit("-v1:0", 1)[0]
    elif "-v" in model_part:
        # Handle other version formats
        model_part = model_part.rsplit("-v", 1)[0]

    return model_part
