"""Dedicated machine principal; its only scope is provider-credential:read.

Provision SANDBOX_BROKER_TOKEN only in platform-api, daemon and passthrough.
Never resolve an app/body/header identity into this privilege.
"""

import hmac
import os
from dataclasses import dataclass

from fastapi import Header, HTTPException

from src.config import config


@dataclass(frozen=True)
class CredentialBroker:
    principal: str = "sandbox-broker"
    scope: str = "provider-credential:read"


def require_credential_broker(
    x_sandbox_broker_token: str | None = Header(default=None),
) -> CredentialBroker:
    expected = os.environ.get("SANDBOX_BROKER_TOKEN", "")
    # Shared app credentials must never also be the broker credential.
    if not expected or expected == config.service_token:
        raise HTTPException(403, "provider-credential:read unavailable")
    if not x_sandbox_broker_token or not hmac.compare_digest(
        x_sandbox_broker_token, expected
    ):
        raise HTTPException(403, "provider-credential:read requires sandbox-broker")
    return CredentialBroker()
