"""
Tenant Request Signing for AI-Bridge

Signs requests with HMAC-SHA256 to authenticate tenant identity.
The Bridge validates this signature to track usage per tenant.

Required env var: BRIDGE_TENANT_SECRET (64-char hex)

Usage:
    from tenant_signing import sign_tenant_request

    headers = sign_tenant_request("engelmann")
    # {'X-Tenant-ID': 'engelmann', 'X-Tenant-Timestamp': '...', 'X-Tenant-Signature': '...'}
"""

import hmac
import hashlib
import os
import time
import logging

logger = logging.getLogger(__name__)


def sign_tenant_request(tenant_id: str) -> dict:
    """
    Generate signed headers for AI-Bridge tenant authentication.

    Args:
        tenant_id: The tenant ID (e.g., 'engelmann', 'teufel-safety')

    Returns:
        Dict with X-Tenant-ID, X-Tenant-Timestamp, X-Tenant-Signature headers.
        Returns empty dict if BRIDGE_TENANT_SECRET is not configured.
    """
    secret = os.environ.get('BRIDGE_TENANT_SECRET')
    if not secret:
        logger.warning('BRIDGE_TENANT_SECRET not configured, skipping signature')
        return {}

    timestamp = str(int(time.time()))
    payload = f"{tenant_id}:{timestamp}"
    signature = hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    return {
        'X-Tenant-ID': tenant_id,
        'X-Tenant-Timestamp': timestamp,
        'X-Tenant-Signature': signature,
    }


def validate_tenant_signature(
    tenant_id: str,
    timestamp: str,
    signature: str,
    max_age_seconds: int = 300
) -> bool:
    """
    Validate a signed tenant request (for testing/debugging).

    Args:
        tenant_id: Expected tenant ID
        timestamp: Timestamp from header (Unix seconds)
        signature: Signature from header (hex)
        max_age_seconds: Maximum age of timestamp (default: 300 = 5 minutes)

    Returns:
        True if valid, False otherwise
    """
    secret = os.environ.get('BRIDGE_TENANT_SECRET')
    if not secret:
        return False

    # Check timestamp age
    try:
        ts = int(timestamp)
        now = int(time.time())
        if abs(now - ts) > max_age_seconds:
            return False
    except ValueError:
        return False

    # Validate signature
    payload = f"{tenant_id}:{timestamp}"
    expected = hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected)
