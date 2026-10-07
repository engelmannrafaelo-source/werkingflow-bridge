"""Explicit opt-in for Energy API keys, independent of normal authentication."""

import hashlib
import hmac
import os

from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials


def erkunder_schluessel_erlaubt(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    allowed = os.environ.get("ERKUNDER_ALLOWED_KEY_SHA256", "").split(",")
    digest = (
        hashlib.sha256(credentials.credentials.encode()).hexdigest()
        if credentials
        else ""
    )
    if not digest or not any(
        hmac.compare_digest(digest, item.strip().lower())
        for item in allowed
        if item.strip()
    ):
        raise HTTPException(403, "API-Schlüssel ist für Erkunder nicht freigegeben")


def intern_config() -> tuple[str, dict[str, str]]:
    url = os.environ.get("ERKUNDER_URL", "").rstrip("/")
    token = os.environ.get("ERKUNDER_INTERNAL_TOKEN", "")
    if not url or not token:
        raise RuntimeError(
            "Erkunder: ERKUNDER_URL und ERKUNDER_INTERNAL_TOKEN erforderlich"
        )
    return url, {"X-Erkunder-Intern": token}
