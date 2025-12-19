import os
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv

from config.logging_config import get_logger

logger = get_logger(__name__)
load_dotenv()


class TokenRotator:
    """Manages multiple OAuth tokens with automatic fallback.

    When one token fails (rate limit, auth error), automatically
    switches to the next available token.

    Token files are read from CLAUDE_CODE_OAUTH_TOKEN_DIR or /run/secrets/
    Files matching pattern: claude_token*.txt
    """

    def __init__(self):
        self.tokens: List[str] = []
        self.current_index: int = 0
        self.token_files: List[Path] = []
        self._load_tokens()

    def _load_tokens(self):
        """Load all available tokens from files."""
        # Primary token file
        primary_file = os.getenv("CLAUDE_CODE_OAUTH_TOKEN_FILE")
        if primary_file:
            primary_path = Path(primary_file)
            if primary_path.exists():
                self.token_files.append(primary_path)

        # Look for additional tokens in secrets directory
        secrets_dirs = [
            Path("/run/secrets"),
            Path("/app/secrets"),
            Path(os.getenv("CLAUDE_CODE_OAUTH_TOKEN_DIR", "/run/secrets"))
        ]

        for secrets_dir in secrets_dirs:
            if secrets_dir.exists():
                # Find all claude_token* files (with or without .txt extension)
                # Docker secrets don't have .txt extension
                for token_file in secrets_dir.glob("claude_token*"):
                    # Skip directories and example files
                    if token_file.is_file() and "example" not in token_file.name:
                        if token_file not in self.token_files:
                            self.token_files.append(token_file)

        # Load tokens from files
        for token_file in self.token_files:
            try:
                token = token_file.read_text().strip()
                if token and token not in self.tokens:
                    self.tokens.append(token)
                    logger.info(f"✅ Loaded token from {token_file.name} ({token[:20]}...)")
            except Exception as e:
                logger.warning(f"⚠️ Failed to read {token_file}: {e}")

        if self.tokens:
            logger.info(f"🔑 TokenRotator initialized with {len(self.tokens)} tokens")
        else:
            logger.warning("⚠️ TokenRotator: No tokens found!")

    def get_current_token(self) -> Optional[str]:
        """Get the currently active token."""
        if not self.tokens:
            return None
        return self.tokens[self.current_index]

    def rotate_token(self) -> Optional[str]:
        """Switch to the next available token.

        Returns the new token, or None if no more tokens available.
        """
        if len(self.tokens) <= 1:
            logger.warning("⚠️ No fallback tokens available!")
            return None

        old_index = self.current_index
        self.current_index = (self.current_index + 1) % len(self.tokens)

        new_token = self.tokens[self.current_index]

        # Update environment variable
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = new_token

        logger.warning(f"🔄 Token rotated: index {old_index} → {self.current_index}")
        logger.info(f"🔑 New active token: {new_token[:20]}...")

        return new_token

    def mark_token_failed(self, error_msg: str = "") -> Optional[str]:
        """Mark current token as failed and rotate to next.

        Call this when you detect rate limiting or auth errors.
        Returns the new token, or None if no more tokens.
        """
        logger.error(f"❌ Token failed: {self.tokens[self.current_index][:20]}... - {error_msg}")
        return self.rotate_token()

    def apply_current_token(self):
        """Apply the current token to environment."""
        token = self.get_current_token()
        if token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
            logger.info(f"✅ Applied token to environment: {token[:20]}...")


# Global token rotator instance
token_rotator = TokenRotator()

# Apply the first token to environment on module load
if token_rotator.tokens:
    token_rotator.apply_current_token()
    logger.info(f"🔑 TokenRotator: {len(token_rotator.tokens)} tokens available, first applied")


class ClaudeCodeAuthManager:
    """Manages authentication for Claude Code SDK integration.

    IMPORTANT FOR LLMs/AI AGENTS:
    ============================
    This wrapper MUST use Claude CLI OAuth authentication ONLY.

    DO NOT use ANTHROPIC_API_KEY - it causes token costs!
    DO NOT set ANTHROPIC_API_KEY in environment variables!

    The wrapper is designed to use the active Claude Code session (OAuth)
    which provides free, unlimited access without consuming API tokens.

    If you see 'anthropic' auth method, it means ANTHROPIC_API_KEY is set
    somewhere in the environment - this is WRONG and must be removed!

    Expected auth method: 'claude_cli' (OAuth, free, no token costs)
    """

    def __init__(self):
        self.env_api_key = os.getenv("API_KEY")  # Environment API key (for FastAPI protection, NOT Claude auth)
        self.auth_method = self._detect_auth_method()
        self.auth_status = self._validate_auth_method()
    
    def get_api_key(self):
        """Get the active API key (environment or runtime-generated)."""
        # Try to import runtime_api_key from main module
        try:
            from src import main
            if hasattr(main, 'runtime_api_key') and main.runtime_api_key:
                return main.runtime_api_key
        except ImportError:
            pass

        # Fall back to environment variable
        return self.env_api_key
    
    def _detect_auth_method(self) -> str:
        """Detect which Claude Code authentication method is configured.

        CRITICAL SECURITY: NO AUTOMATIC FALLBACK TO ANTHROPIC_API_KEY!
        ===============================================================
        This wrapper ONLY supports:
        - Claude CLI OAuth (free, no token costs) - DEFAULT
        - AWS Bedrock (CLAUDE_CODE_USE_BEDROCK=1)
        - Google Vertex (CLAUDE_CODE_USE_VERTEX=1)

        If ANTHROPIC_API_KEY is detected, it will be REMOVED from environment
        to prevent Claude CLI from using it as a fallback. This prevents
        unexpected API costs when OAuth has issues.

        NEVER silently fall back to paid API - always fail loud with clear error!
        """
        # CRITICAL: Remove ANTHROPIC_API_KEY from environment to prevent fallback
        # This prevents Claude CLI from silently using API key when OAuth fails
        # BUT: Save it for Vision/Image analysis which requires direct API access
        if os.getenv("ANTHROPIC_API_KEY"):
            # Save for Vision before removing
            vision_key = os.getenv("ANTHROPIC_API_KEY")
            os.environ["ANTHROPIC_VISION_API_KEY"] = vision_key

            logger.warning("=" * 70)
            logger.warning("⚠️  ANTHROPIC_API_KEY DETECTED - MOVING TO VISION-ONLY!")
            logger.warning("=" * 70)
            logger.warning("This wrapper uses OAuth for Claude CLI (no token costs).")
            logger.warning("ANTHROPIC_API_KEY saved as ANTHROPIC_VISION_API_KEY for image analysis.")
            logger.warning("Claude CLI will NOT use this key - OAuth only!")
            logger.warning("=" * 70)
            # CRITICAL: Remove from main env so Claude CLI doesn't use it
            del os.environ["ANTHROPIC_API_KEY"]

        if os.getenv("CLAUDE_CODE_USE_BEDROCK") == "1":
            return "bedrock"
        elif os.getenv("CLAUDE_CODE_USE_VERTEX") == "1":
            return "vertex"
        else:
            # ALWAYS use Claude CLI OAuth (free, no token costs)
            return "claude_cli"
    
    def _validate_auth_method(self) -> Dict[str, Any]:
        """Validate the detected authentication method."""
        method = self.auth_method
        status = {
            "method": method,
            "valid": False,
            "errors": [],
            "config": {}
        }

        if method == "bedrock":
            status.update(self._validate_bedrock_auth())
        elif method == "vertex":
            status.update(self._validate_vertex_auth())
        elif method == "claude_cli":
            status.update(self._validate_claude_cli_auth())
        else:
            status["errors"].append("No Claude Code authentication method configured")

        return status
    
    
    def _validate_bedrock_auth(self) -> Dict[str, Any]:
        """Validate AWS Bedrock authentication."""
        errors = []
        config = {}
        
        # Check if Bedrock is enabled
        if os.getenv("CLAUDE_CODE_USE_BEDROCK") != "1":
            errors.append("CLAUDE_CODE_USE_BEDROCK must be set to '1'")
        
        # Check AWS credentials
        aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
        aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        aws_region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION"))
        
        if not aws_access_key:
            errors.append("AWS_ACCESS_KEY_ID environment variable not set")
        if not aws_secret_key:
            errors.append("AWS_SECRET_ACCESS_KEY environment variable not set")
        if not aws_region:
            errors.append("AWS_REGION or AWS_DEFAULT_REGION environment variable not set")
        
        config.update({
            "aws_access_key_present": bool(aws_access_key),
            "aws_secret_key_present": bool(aws_secret_key),
            "aws_region": aws_region,
        })
        
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "config": config
        }
    
    def _validate_vertex_auth(self) -> Dict[str, Any]:
        """Validate Google Vertex AI authentication."""
        errors = []
        config = {}
        
        # Check if Vertex is enabled
        if os.getenv("CLAUDE_CODE_USE_VERTEX") != "1":
            errors.append("CLAUDE_CODE_USE_VERTEX must be set to '1'")
        
        # Check required Vertex AI environment variables
        project_id = os.getenv("ANTHROPIC_VERTEX_PROJECT_ID")
        region = os.getenv("CLOUD_ML_REGION")
        
        if not project_id:
            errors.append("ANTHROPIC_VERTEX_PROJECT_ID environment variable not set")
        if not region:
            errors.append("CLOUD_ML_REGION environment variable not set")
        
        config.update({
            "project_id": project_id,
            "region": region,
        })
        
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "config": config
        }
    
    def _validate_claude_cli_auth(self) -> Dict[str, Any]:
        """Validate that Claude Code CLI is already authenticated."""
        # For CLI authentication, we assume it's valid and let the SDK handle auth
        # The actual validation will happen when we try to use the SDK
        return {
            "valid": True,
            "errors": [],
            "config": {
                "method": "Claude Code CLI authentication",
                "note": "Using existing Claude Code CLI authentication"
            }
        }
    
    def get_claude_code_env_vars(self) -> Dict[str, str]:
        """Get environment variables needed for Claude Code SDK.

        IMPORTANT: ANTHROPIC_API_KEY support has been REMOVED!
        This wrapper ONLY supports Claude CLI OAuth authentication.

        Supports Docker Secrets via CLAUDE_CODE_OAUTH_TOKEN_FILE.
        """
        env_vars = {}

        if self.auth_method == "bedrock":
            env_vars["CLAUDE_CODE_USE_BEDROCK"] = "1"
            if os.getenv("AWS_ACCESS_KEY_ID"):
                env_vars["AWS_ACCESS_KEY_ID"] = os.getenv("AWS_ACCESS_KEY_ID")
            if os.getenv("AWS_SECRET_ACCESS_KEY"):
                env_vars["AWS_SECRET_ACCESS_KEY"] = os.getenv("AWS_SECRET_ACCESS_KEY")
            if os.getenv("AWS_REGION"):
                env_vars["AWS_REGION"] = os.getenv("AWS_REGION")

        elif self.auth_method == "vertex":
            env_vars["CLAUDE_CODE_USE_VERTEX"] = "1"
            if os.getenv("ANTHROPIC_VERTEX_PROJECT_ID"):
                env_vars["ANTHROPIC_VERTEX_PROJECT_ID"] = os.getenv("ANTHROPIC_VERTEX_PROJECT_ID")
            if os.getenv("CLOUD_ML_REGION"):
                env_vars["CLOUD_ML_REGION"] = os.getenv("CLOUD_ML_REGION")
            if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                env_vars["GOOGLE_APPLICATION_CREDENTIALS"] = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

        elif self.auth_method == "claude_cli":
            # Docker Secrets support: Read token from file if specified
            token_file = os.getenv("CLAUDE_CODE_OAUTH_TOKEN_FILE")
            if token_file:
                try:
                    from pathlib import Path
                    token = Path(token_file).read_text().strip()
                    # CRITICAL: Set ENV variable IMMEDIATELY for SDK to find it
                    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
                    env_vars["CLAUDE_CODE_OAUTH_TOKEN"] = token
                    logger.info(f"✅ OAuth token loaded from file and set in environment: {token_file}")
                except Exception as e:
                    logger.error(f"❌ Failed to read OAuth token from {token_file}: {e}")
                    raise  # Fail loud, don't continue with invalid auth
            # Otherwise, let Claude Code SDK use the existing CLI authentication

        return env_vars


# Initialize the auth manager
auth_manager = ClaudeCodeAuthManager()

# HTTP Bearer security scheme (for FastAPI endpoint protection)
security = HTTPBearer(auto_error=False)


async def verify_api_key(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = None):
    """
    Verify API key if one is configured for FastAPI endpoint protection.
    This is separate from Claude Code authentication.
    """
    # Get the active API key (environment or runtime-generated)
    active_api_key = auth_manager.get_api_key()
    
    # If no API key is configured, allow all requests
    if not active_api_key:
        return True
    
    # Get credentials from Authorization header
    if credentials is None:
        credentials = await security(request)
    
    # Check if credentials were provided
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Verify the API key
    if credentials.credentials != active_api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return True


def validate_claude_code_auth() -> Tuple[bool, Dict[str, Any]]:
    """
    Validate Claude Code authentication and return status.
    Returns (is_valid, status_info)
    """
    status = auth_manager.auth_status
    
    if not status["valid"]:
        logger.error(f"Claude Code authentication failed: {status['errors']}")
        return False, status
    
    logger.info(f"Claude Code authentication validated: {status['method']}")
    return True, status


def get_claude_code_auth_info() -> Dict[str, Any]:
    """Get Claude Code authentication information for diagnostics."""
    return {
        "method": auth_manager.auth_method,
        "status": auth_manager.auth_status,
        "environment_variables": list(auth_manager.get_claude_code_env_vars().keys())
    }