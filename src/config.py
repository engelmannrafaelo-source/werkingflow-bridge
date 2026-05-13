"""
Bridge configuration — single source of truth for env-driven settings.

Loaded once at import time. Each setting fails LOUD when its precondition
is violated (defensive coding, fail-fast). NEVER returns a fallback for a
production-required value.

Usage:
    from src.config import config
    secret = config.jwt_secret          # raises if BRIDGE_JWT_SECRET unset
    url = config.public_url             # raises if BRIDGE_PUBLIC_URL unset

All boolean envs follow the same parsing rule: "1", "true", "yes", "on" (case-insensitive).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


_TRUE = {"1", "true", "yes", "on"}


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _required(name: str) -> str:
    val = os.getenv(name)
    if not val or not val.strip():
        raise RuntimeError(
            f"{name} is required but not set. "
            f"Refusing to start in an unsafe configuration."
        )
    return val.strip()


@dataclass(frozen=True)
class BridgeConfig:
    """Immutable runtime configuration. Read once, never mutate."""

    # -- DB --------------------------------------------------------------
    db_url: str | None
    """Postgres connection string. If None, DB-backed routes are not mounted."""

    # -- Identity --------------------------------------------------------
    jwt_secret_raw: str | None
    """Raw value; use `jwt_secret` property for fail-fast access."""

    # -- Billing / Mollie ------------------------------------------------
    use_fake_mollie: bool
    """If True: in-memory FakeMollieAdapter. Required to be False on Hetzner production."""

    public_url_raw: str | None
    """Raw value; use `public_url` property for fail-fast access."""

    mollie_api_key_raw: str | None

    # -- Internal service auth ------------------------------------------
    service_token_raw: str | None
    """Shared secret for internal service-to-service calls (topup/credit, webhooks-from-self)."""

    @property
    def db_enabled(self) -> bool:
        return self.db_url is not None and self.db_url != ""

    @property
    def jwt_secret(self) -> str:
        """Fails loud if not set. Call only when DB-backed routes are active."""
        if not self.jwt_secret_raw:
            raise RuntimeError(
                "BRIDGE_JWT_SECRET is required when DB-backed routes are mounted."
            )
        return self.jwt_secret_raw

    @property
    def public_url(self) -> str:
        """Bridge's externally reachable URL — used for Mollie webhook callbacks."""
        if not self.public_url_raw:
            raise RuntimeError(
                "BRIDGE_PUBLIC_URL is required for billing endpoints. "
                "Set it to e.g. https://bridge.werking.tools (prod) or http://localhost:8100 (mirror)."
            )
        return self.public_url_raw.rstrip("/")

    @property
    def mollie_webhook_url(self) -> str:
        return f"{self.public_url}/v1/billing/mollie-webhook"

    @property
    def service_token(self) -> str:
        """Fails loud if not set. Internal endpoints require this token."""
        if not self.service_token_raw:
            raise RuntimeError(
                "BRIDGE_SERVICE_TOKEN is required for internal service endpoints."
            )
        return self.service_token_raw


def _load() -> BridgeConfig:
    cfg = BridgeConfig(
        db_url=os.getenv("BRIDGE_DB_URL") or None,
        jwt_secret_raw=os.getenv("BRIDGE_JWT_SECRET") or None,
        use_fake_mollie=_bool("BRIDGE_USE_FAKE_MOLLIE", default=False),
        public_url_raw=os.getenv("BRIDGE_PUBLIC_URL") or None,
        mollie_api_key_raw=os.getenv("MOLLIE_API_KEY") or None,
        service_token_raw=os.getenv("BRIDGE_SERVICE_TOKEN") or None,
    )

    if cfg.db_enabled:
        # DB-backed routes (identity/budget/billing/activity/admin) need these.
        # Fail loudly at boot rather than at first request.
        _ = cfg.jwt_secret           # raises if missing
        _ = cfg.service_token        # raises if missing
        _ = cfg.public_url           # raises if missing

        if not cfg.use_fake_mollie and not cfg.mollie_api_key_raw:
            raise RuntimeError(
                "MOLLIE_API_KEY is required when BRIDGE_USE_FAKE_MOLLIE is not true."
            )

    return cfg


config: BridgeConfig = _load()
