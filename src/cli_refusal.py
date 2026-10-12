"""The CLI's refusal follow-ups and its refusal system messages (BRREFUSAL,
FK32 b; BRREFUSALR P1). Shared by run_completion (src/claude_cli.py) and the
explorer's CLI path (src/erkunder/kind.py), which runs in its own image with
only src/erkunder, src/sdk_parser.py and this module — so stdlib only.

Background and measurements: the block above apply_refusal_retry_policy in
src/claude_cli.py."""

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CLI_REFUSAL_NO_FALLBACK_SUBTYPE = "model_refusal_no_fallback"
CLI_REFUSAL_FALLBACK_SUBTYPE = "model_refusal_fallback"
CLI_DISABLE_REFUSAL_RETRY_ENV = "CLAUDE_CODE_DISABLE_REFUSAL_RETRY"
CLI_DISABLE_REFUSAL_FALLBACK_ENV = "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"
# Switches for the bridge: "true" lets the CLI retry on the same model / fall
# back to another model after a refusal again (pre-BRREFUSAL behaviour).
# Default: both off.
BRIDGE_CLI_REFUSAL_RETRY_ENV = "BRIDGE_CLI_REFUSAL_RETRY"
BRIDGE_CLI_REFUSAL_FALLBACK_ENV = "BRIDGE_CLI_REFUSAL_FALLBACK"


def _env_true(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in ("true", "1", "yes")


def cli_refusal_retry_allowed() -> bool:
    return _env_true(BRIDGE_CLI_REFUSAL_RETRY_ENV)


def cli_refusal_fallback_allowed() -> bool:
    return _env_true(BRIDGE_CLI_REFUSAL_FALLBACK_ENV)


def refusal_policy_env() -> Dict[str, str]:
    """The CLI env switches for this run: both refusal follow-ups off unless
    the bridge switch allows the one or the other. Shared by run_completion
    and the explorer's CLI path (src/erkunder/kind.py)."""
    env: Dict[str, str] = {}
    if cli_refusal_retry_allowed():
        logger.info(f"↩️ {BRIDGE_CLI_REFUSAL_RETRY_ENV}=true — CLI may retry after a refusal")
    else:
        env[CLI_DISABLE_REFUSAL_RETRY_ENV] = "1"
    if cli_refusal_fallback_allowed():
        logger.info(
            f"↩️ {BRIDGE_CLI_REFUSAL_FALLBACK_ENV}=true — CLI may fall back to another "
            f"model after a refusal (the run still ends as modell_abbruch)"
        )
    else:
        env[CLI_DISABLE_REFUSAL_FALLBACK_ENV] = "1"
    return env


def cli_refusal_signal(message: Any) -> Optional[Dict[str, Any]]:
    """{stop_reason, category, quelle} if `message` is one of the CLI's
    refusal system messages, else None:
    - "model_refusal_no_fallback": refused, nothing follows;
    - "model_refusal_fallback" (trigger "refusal"): refused, and the CLI moves
      on to another model (or back) — then also original_model,
      fallback_model, direction. The run is a refusal either way: what the
      other model writes is not an answer to the request under its model.
    quelle = the subtype. SDK form: SystemMessage(subtype, data); dict form:
    {"type": "system", "subtype": ..., ...} or with "data"."""
    if message is None:
        return None
    if isinstance(message, dict):
        subtype = message.get("subtype")
        data = message.get("data") if isinstance(message.get("data"), dict) else message
        if message.get("type") not in (None, "system") and data.get("type") != "system":
            return None
    else:
        if type(message).__name__ != "SystemMessage":
            return None
        subtype = getattr(message, "subtype", None)
        data = getattr(message, "data", None)
        data = data if isinstance(data, dict) else {}
    if subtype == CLI_REFUSAL_NO_FALLBACK_SUBTYPE:
        return {"stop_reason": "refusal", "category": data.get("api_refusal_category"),
                "quelle": CLI_REFUSAL_NO_FALLBACK_SUBTYPE}
    if subtype == CLI_REFUSAL_FALLBACK_SUBTYPE:
        return {"stop_reason": "refusal", "category": data.get("api_refusal_category"),
                "quelle": CLI_REFUSAL_FALLBACK_SUBTYPE,
                "direction": data.get("direction"),
                "original_model": data.get("original_model"),
                "fallback_model": data.get("fallback_model")}
    return None
