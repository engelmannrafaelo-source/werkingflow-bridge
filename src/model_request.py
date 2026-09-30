"""Adapt legacy request controls for always-on adaptive thinking models."""
from config.logging_config import get_logger

logger = get_logger(__name__)


def adapt_model_request(model: str, body: dict) -> None:
    """Normalize the outgoing Messages API body; leave other models untouched.

    Opus 5.5 and Fable 5.1 always think. Legacy disabled thinking maps to low effort;
    manual budgets map to high effort. Explicit effort always wins.
    Invalid thinking types remain visible as provider validation errors.
    """
    if model == "claude-sonnet-5-5":
        # Sonnet 5.5 rejects disabled/manual thinking and sampling controls.
        # Preserve explicit effort: incompatible between_tools/xhigh remains a
        # provider validation error instead of silently changing user intent.
        for key in ("temperature", "top_p", "top_k"):
            body.pop(key, None)
        thinking = body.get("thinking") or {}
        if thinking.get("type") == "disabled":
            body["thinking"] = {"type": "between_tools"}
        elif thinking.get("type") == "enabled":
            body["thinking"] = {
                "type": "adaptive",
                **({"display": thinking["display"]} if "display" in thinking else {}),
            }
        body["output_config"] = {"effort": "high", **(body.get("output_config") or {})}
        return
    if model not in ("claude-opus-5-5", "claude-fable-5-1"):
        return
    for key in ("temperature", "top_p", "top_k"):
        body.pop(key, None)
    thinking = body.get("thinking") or {}
    effort = "high" if model == "claude-fable-5-1" else "medium"
    if thinking.get("type") in ("disabled", "enabled"):
        effort = "low" if thinking["type"] == "disabled" else "high"
        body["thinking"] = {"type": "adaptive"}
        logger.warning("%s requires adaptive thinking; mapping legacy %s to %s effort",
                       model, thinking["type"], effort)
    body["output_config"] = {"effort": effort, **(body.get("output_config") or {})}
