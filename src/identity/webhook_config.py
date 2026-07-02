"""
Webhook config — Bridge → App auth-token webhook URLs + HMAC secrets.

Implements ADR cross-app/0002 Phase M1: instead of stdout-logging cleartext
tokens, the Bridge POSTs them HMAC-signed to a per-app receiver URL. Each
app that uses Bridge-Auth must provide:

    BRIDGE_WEBHOOK_URL_<APP_ID_UPPER>      e.g. BRIDGE_WEBHOOK_URL_WERKING_REPORT
    BRIDGE_WEBHOOK_SECRET_<APP_ID_UPPER>   shared HMAC secret with the app

`<APP_ID_UPPER>` is the app_id with dashes mapped to underscores and uppercased:

    werking-report  -> WERKING_REPORT
    werking-energy  -> WERKING_ENERGY
    werking-safety  -> WERKING_SAFETY
    werking-noise   -> WERKING_NOISE

Apps that do NOT use Bridge-Auth (engelmann is on Supabase per ADR cross-app/0002)
are NOT in `BRIDGE_AUTH_APP_IDS` and therefore not boot-validated.

Design: bootstrap-only via env (Option B1 in the task brief). No admin endpoint
to mutate at runtime — every change goes through Infisical + a restart. That's
operations-hygiene: a stale or wrong webhook URL is an outage of the password-
reset / email-verification flow, and we want operator review on every change.

Fail-loud at boot: a missing URL or secret for a Bridge-Auth app raises
RuntimeError before the FastAPI app accepts any traffic. Same posture as
src/config.py (jwt_secret, service_token, public_url): the Bridge refuses to
start in an unsafe configuration.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)


# Apps whose auth flows go through the Bridge AND have a working webhook
# receiver implemented + reachable. Phase M1 rollout is gradual — only
# werking-report is wired up today (commit adds /api/auth/webhook/token-issued
# + uses existing src/lib/email.ts pipeline).
#
# To add another app: implement the same receiver route, configure
# BRIDGE_WEBHOOK_URL_<APP_UPPER> + BRIDGE_WEBHOOK_SECRET_<APP_UPPER> in
# Infisical dev-server, then add the app_id here. Bridge fail-loud at boot
# verifies the env vars on every reachable app — so the wiring is forced
# to be complete before the app joins this set.
#
# Engelmann is on Supabase (per ADR cross-app/0002) and is intentionally
# absent. _REGISTER_ALLOWED_APP_IDS in src/identity/routes.py is a SEPARATE
# set (registration is allowed for more apps than webhook-mail is wired up
# for — they just stdout-log the token until their receiver lands).
# Default set applied when the BRIDGE_AUTH_APP_IDS env var is unset/empty —
# preserves the historical hardcoded behaviour so nothing changes on a plain
# rebuild without the env var.
_DEFAULT_BRIDGE_AUTH_APP_IDS = "werking-report"


def _load_bridge_auth_app_ids() -> FrozenSet[str]:
    """Apps whose users must verify their email before login.

    Read from the ``BRIDGE_AUTH_APP_IDS`` env var (comma-separated) so arming a
    new app becomes an env change + recreate — NOT an image rebuild. Falls back
    to the historical hardcoded default when the env var is absent or empty, so
    behaviour is unchanged unless the env var is explicitly set. Whitespace and
    empty entries are ignored.
    """
    raw = os.environ.get("BRIDGE_AUTH_APP_IDS", "").strip() or _DEFAULT_BRIDGE_AUTH_APP_IDS
    return frozenset(app_id.strip() for app_id in raw.split(",") if app_id.strip())


BRIDGE_AUTH_APP_IDS: FrozenSet[str] = _load_bridge_auth_app_ids()


@dataclass(frozen=True)
class WebhookConfig:
    """Per-app webhook endpoint + shared HMAC secret. Immutable after boot."""

    url: str
    secret: str


def _env_name(app_id: str, suffix: str) -> str:
    """Map ('werking-report', 'URL') -> 'BRIDGE_WEBHOOK_URL_WERKING_REPORT'."""
    upper = app_id.replace("-", "_").upper()
    return f"BRIDGE_WEBHOOK_{suffix}_{upper}"


def load_webhook_configs(
    app_ids: FrozenSet[str] = BRIDGE_AUTH_APP_IDS,
) -> Dict[str, WebhookConfig]:
    """
    Read webhook URL + secret for every required app from the environment.

    Raises RuntimeError listing ALL missing env vars at once — operators
    fixing a config typo would otherwise have to restart-fail-fix-restart
    per app.

    Empty strings ("set but blank") count as missing. A secret of zero
    length would silently make HMAC verification trivially forgeable.
    """
    missing: list[str] = []
    configs: Dict[str, WebhookConfig] = {}

    for app_id in sorted(app_ids):
        url_var = _env_name(app_id, "URL")
        secret_var = _env_name(app_id, "SECRET")

        url = (os.getenv(url_var) or "").strip()
        secret = (os.getenv(secret_var) or "").strip()

        if not url:
            missing.append(url_var)
        if not secret:
            missing.append(secret_var)

        if url and secret:
            configs[app_id] = WebhookConfig(url=url, secret=secret)

    if missing:
        raise RuntimeError(
            "Bridge webhook config incomplete. The following environment "
            "variables are required (Infisical dev-server project) but unset "
            "or empty: " + ", ".join(missing) +
            ". Bridge refuses to start in an unsafe configuration — see "
            "ADR cross-app/0002 (Bridge-Token Mail-Pipeline)."
        )

    return configs


# Module-level cache, populated by `init_webhook_configs()` during lifespan
# startup. Access through `get_webhook_config(app_id)` which fail-louds when
# `init` was skipped.
_configs: Optional[Dict[str, WebhookConfig]] = None


def init_webhook_configs(
    app_ids: FrozenSet[str] = BRIDGE_AUTH_APP_IDS,
) -> Dict[str, WebhookConfig]:
    """
    Load and cache webhook configs. Idempotent — repeated calls re-load (so
    a test can monkeypatch env vars and re-init). Returns the live cache so
    the caller can log how many apps are wired up.
    """
    global _configs
    _configs = load_webhook_configs(app_ids)
    logger.info(
        "webhook_config: loaded %d app(s): %s",
        len(_configs),
        sorted(_configs.keys()),
    )
    return _configs


def get_webhook_config(app_id: str) -> WebhookConfig:
    """
    Fetch the webhook config for an app_id. Used by both the route hook
    (token-issue time) and the dispatcher worker (delivery time).

    Raises LookupError when the app_id is unknown (i.e. not in
    BRIDGE_AUTH_APP_IDS — would happen if a route handler passed an
    unexpected app_id, e.g. 'engelmann' which is Supabase-only). The
    error is caught and translated to HTTP 503 by the route to keep the
    fail-loud posture: the user issued a token but the system can't
    deliver it, which means do NOT pretend success.

    Raises RuntimeError when init_webhook_configs() has not run — this is
    only ever a programmer error (forgot to wire into lifespan).
    """
    if _configs is None:
        raise RuntimeError(
            "webhook_config: init_webhook_configs() was never called. "
            "platform_main.lifespan must call it on startup."
        )
    cfg = _configs.get(app_id)
    if cfg is None:
        raise LookupError(
            f"webhook_config: no webhook configured for app_id={app_id!r}. "
            f"Known apps: {sorted(_configs.keys())}"
        )
    return cfg


def reset_for_tests() -> None:
    """Test-only: clear the cache so a subsequent `init_webhook_configs()`
    re-reads the env. Safe to call from anywhere; production code does not."""
    global _configs
    _configs = None
