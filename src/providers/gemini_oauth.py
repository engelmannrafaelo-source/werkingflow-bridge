"""Google Gemini OAuth Manager — Token refresh for Gemini free tier.

Manages OAuth credentials for the Gemini API's OpenAI-compatible endpoint.
Access tokens expire in 1 hour, auto-refreshed at 55min mark.
Free tier: 1.000 req/day, 60 req/min — tracked in memory.

Supports two credential formats:
1. Bridge format (from setup-gemini-oauth.py):
   {"token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes"}
2. Native Gemini CLI format (from ~/.gemini/oauth_creds.json):
   {"access_token", "refresh_token", "scope", "token_type", "expiry_date"}

Credential file search (priority order):
1. GEMINI_OAUTH_CREDS_FILE env var
2. /run/secrets/gemini_oauth_creds.json (Docker)
3. secrets/gemini_oauth_creds.json (local, relative to bridge root)
"""

import os
import json
import time
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Bridge root directory (src/providers/ → src/ → bridge/)
BRIDGE_ROOT = Path(__file__).parent.parent.parent

# Gemini CLI OAuth Client credentials (loaded from environment)
# Set GEMINI_OAUTH_CLIENT_ID and GEMINI_OAUTH_CLIENT_SECRET in env or Docker secrets
_DEFAULT_CLIENT_ID = os.environ.get("GEMINI_OAUTH_CLIENT_ID", "")
_DEFAULT_CLIENT_SECRET = os.environ.get("GEMINI_OAUTH_CLIENT_SECRET", "")
_DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"


@dataclass
class GeminiTokens:
    """Gemini OAuth token state."""
    access_token: str
    refresh_token: str
    token_uri: str
    client_id: str
    client_secret: str
    expires_at: float  # Unix timestamp
    scopes: list


class GeminiOAuthManager:
    """Manages Google Gemini OAuth tokens with automatic refresh.

    Token lifecycle:
    - Access token: Expires in 1 hour, refreshed proactively at 55min mark
    - Refresh token: Long-lived (indefinite unless revoked or 6 months inactive)

    Rate limits (free tier, shared across all Gemini models):
    - 1.000 requests per day
    - 60 requests per minute
    """

    def __init__(self):
        self.tokens: Optional[GeminiTokens] = None
        self.daily_requests: int = 0
        self.daily_limit: int = 1000
        self._minute_timestamps: list[float] = []
        self.minute_limit: int = 60
        self._load_credentials()

    def _find_credentials_file(self) -> Optional[Path]:
        """Find OAuth credentials file in priority order."""
        # 1. Environment variable
        env_path = os.getenv("GEMINI_OAUTH_CREDS_FILE")
        if env_path:
            p = Path(env_path)
            if p.exists():
                return p
            logger.warning(f"GEMINI_OAUTH_CREDS_FILE set but not found: {env_path}")

        # 2. Docker secrets
        docker_path = Path("/run/secrets/gemini_oauth_creds.json")
        if docker_path.exists():
            return docker_path

        # 3. Local secrets directory
        local_path = BRIDGE_ROOT / "secrets" / "gemini_oauth_creds.json"
        if local_path.exists():
            return local_path

        return None

    def _load_credentials(self):
        """Load OAuth credentials from file.

        Supports two formats:
        1. Bridge format: {"token", "refresh_token", "token_uri", "client_id", "client_secret"}
        2. Native Gemini CLI: {"access_token", "refresh_token", "expiry_date"}
           (client_id/secret/token_uri use hardcoded Gemini CLI defaults)
        """
        creds_path = self._find_credentials_file()

        if not creds_path:
            logger.warning(
                "Gemini OAuth credentials not found. "
                "Providers 'gemini-flash', 'gemini-pro', 'gemini-3-flash', 'gemini-3-pro' will be unavailable. "
                "Run: python3 secrets/setup-gemini-oauth.py"
            )
            return

        try:
            with open(creds_path) as f:
                creds = json.load(f)

            if "refresh_token" not in creds:
                raise ValueError("Missing required field: refresh_token")

            # Detect format: Bridge format has "token", native has "access_token"
            if "token" in creds:
                # Bridge format (from setup-gemini-oauth.py)
                access_token = creds["token"]
            elif "access_token" in creds:
                # Native Gemini CLI format
                access_token = creds["access_token"]
            else:
                access_token = ""

            # Client credentials: use file values or hardcoded Gemini CLI defaults
            client_id = creds.get("client_id", _DEFAULT_CLIENT_ID)
            client_secret = creds.get("client_secret", _DEFAULT_CLIENT_SECRET)
            token_uri = creds.get("token_uri", _DEFAULT_TOKEN_URI)

            # Expiry: Bridge format stores seconds-until-expiry, native stores epoch ms
            expiry = creds.get("expiry_date")
            if expiry and expiry > 1_000_000_000_000:
                # Epoch milliseconds (native Gemini CLI format)
                expires_at = expiry / 1000
            elif expiry and expiry > 0:
                # Seconds until expiry (from setup script)
                expires_at = time.time() + expiry
            else:
                # Force immediate refresh
                expires_at = time.time() - 1 if not access_token else time.time() + 3600

            self.tokens = GeminiTokens(
                access_token=access_token,
                refresh_token=creds["refresh_token"],
                token_uri=token_uri,
                client_id=client_id,
                client_secret=client_secret,
                expires_at=expires_at,
                scopes=creds.get("scopes", creds.get("scope", "").split() if isinstance(creds.get("scope"), str) else []),
            )

            logger.info(f"Gemini OAuth credentials loaded from {creds_path.name}")

        except Exception as e:
            logger.error(f"Failed to load Gemini OAuth credentials from {creds_path}: {e}")
            self.tokens = None

    def is_configured(self) -> bool:
        """Check if OAuth credentials are available."""
        return self.tokens is not None

    def get_access_token(self) -> str:
        """Get current access token, refreshing if needed.

        Returns:
            Valid access token string

        Raises:
            RuntimeError: If not configured or refresh fails
        """
        if not self.tokens:
            raise RuntimeError(
                "Gemini OAuth not configured. "
                "Set GEMINI_OAUTH_CREDS_FILE or run secrets/setup-gemini-oauth.sh"
            )

        # Refresh if token expires within 5 minutes
        if time.time() + 300 > self.tokens.expires_at:
            self._refresh_token()

        return self.tokens.access_token

    def _refresh_token(self):
        """Refresh access token via Google's token endpoint.

        POST https://oauth2.googleapis.com/token
        grant_type=refresh_token

        Raises:
            RuntimeError: If refresh fails (fail loud, no silent fallback)
        """
        if not self.tokens:
            raise RuntimeError("Cannot refresh: Gemini OAuth not configured")

        logger.info("Refreshing Gemini access token...")

        try:
            data = {
                "client_id": self.tokens.client_id,
                "client_secret": self.tokens.client_secret,
                "refresh_token": self.tokens.refresh_token,
                "grant_type": "refresh_token",
            }

            with httpx.Client(timeout=30.0) as client:
                response = client.post(self.tokens.token_uri, data=data)

            if response.status_code != 200:
                error_text = response.text[:500]
                raise RuntimeError(
                    f"Google token refresh failed ({response.status_code}): {error_text}"
                )

            result = response.json()

            if "access_token" not in result:
                raise RuntimeError(
                    f"Token refresh response missing access_token: {list(result.keys())}"
                )

            expires_in = result.get("expires_in", 3600)
            self.tokens.access_token = result["access_token"]
            self.tokens.expires_at = time.time() + expires_in

            logger.info(f"Gemini access token refreshed (expires in {expires_in}s)")

        except httpx.HTTPError as e:
            raise RuntimeError(f"Network error during Gemini token refresh: {e}")

    def check_rate_limit(self):
        """Check if next request would exceed rate limits.

        Raises:
            RuntimeError: If daily or per-minute limit exceeded
        """
        # Daily limit
        if self.daily_requests >= self.daily_limit:
            raise RuntimeError(
                f"Gemini daily rate limit exceeded: {self.daily_requests}/{self.daily_limit}. "
                "Free tier allows 1000 requests per day. Resets at midnight UTC."
            )

        # Per-minute limit (sliding window)
        now = time.time()
        cutoff = now - 60
        self._minute_timestamps = [t for t in self._minute_timestamps if t > cutoff]

        if len(self._minute_timestamps) >= self.minute_limit:
            raise RuntimeError(
                f"Gemini per-minute rate limit exceeded: {len(self._minute_timestamps)}/{self.minute_limit}. "
                "Free tier allows 60 requests per minute."
            )

    def track_request(self):
        """Track a completed request for rate limiting."""
        self.daily_requests += 1
        self._minute_timestamps.append(time.time())

        if self.daily_requests % 100 == 0:
            logger.info(f"Gemini usage: {self.daily_requests}/{self.daily_limit} daily requests")

    def reset_daily_counter(self):
        """Reset daily request counter. Called at midnight UTC."""
        old = self.daily_requests
        self.daily_requests = 0
        if old > 0:
            logger.info(f"Gemini daily counter reset ({old} -> 0)")

    def get_status(self) -> dict:
        """Return current status for /v1/providers endpoint."""
        now = time.time()
        cutoff = now - 60
        minute_count = len([t for t in self._minute_timestamps if t > cutoff])

        return {
            "configured": self.is_configured(),
            "daily_requests": self.daily_requests,
            "daily_limit": self.daily_limit,
            "minute_requests": minute_count,
            "minute_limit": self.minute_limit,
            "token_expires_in": int(self.tokens.expires_at - now) if self.tokens else None,
        }


# Global instance — loaded on module import
gemini_oauth_manager = GeminiOAuthManager()
