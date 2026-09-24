"""Adapt legacy request controls when the Bridge upgrades Opus to 5.5."""
from config.logging_config import get_logger

logger = get_logger(__name__)


def adapt_opus_request(model: str, body: dict) -> None:
    """Normalize the outgoing Messages API body; leave other models untouched.

    Opus 5.5 always thinks. Legacy disabled thinking maps to low effort;
    manual budgets map to high effort. Explicit effort always wins.
    Invalid thinking types remain visible as provider validation errors.
    """
    if model != "claude-opus-5-5":
        return
    for key in ("temperature", "top_p", "top_k"):
        body.pop(key, None)
    thinking = body.get("thinking") or {}
    effort = "medium"
    if thinking.get("type") in ("disabled", "enabled"):
        effort = "low" if thinking["type"] == "disabled" else "high"
        body["thinking"] = {"type": "adaptive"}
        logger.warning("Opus 5.5 requires adaptive thinking; mapping legacy %s to %s effort",
                       thinking["type"], effort)
    body["output_config"] = {"effort": effort, **(body.get("output_config") or {})}
