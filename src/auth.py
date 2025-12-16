import os
from typing import Optional, Dict, Any, Tuple
from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv

from config.logging_config import get_logger

logger = get_logger(__name__)
load_dotenv()


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

        Priority order:
        1. Bedrock (CLAUDE_CODE_USE_BEDROCK=1)
        2. Vertex (CLAUDE_CODE_USE_VERTEX=1)
        3. Docker/Server: ANTHROPIC_API_KEY (when PRIVACY_ENABLED=true or IN_DOCKER=true)
        4. Local: Claude CLI OAuth (free, no token costs)
        """
        if os.getenv("CLAUDE_CODE_USE_BEDROCK") == "1":
            return "bedrock"
        elif os.getenv("CLAUDE_CODE_USE_VERTEX") == "1":
            return "vertex"

        # Docker/Server context: Use ANTHROPIC_API_KEY if available
        is_docker = os.getenv("PRIVACY_ENABLED") == "true" or os.getenv("IN_DOCKER") == "true"
        if is_docker and os.getenv("ANTHROPIC_API_KEY"):
            logger.info("✅ Docker context detected, using ANTHROPIC_API_KEY for authentication")
            return "anthropic_api"

        # Local context: Use Claude CLI OAuth
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
        elif method == "anthropic_api":
            status.update(self._validate_anthropic_api_auth())
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
    
    def _validate_anthropic_api_auth(self) -> Dict[str, Any]:
        """Validate Anthropic API key authentication (Docker/Server context)."""
        errors = []
        config = {}

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            errors.append("ANTHROPIC_API_KEY environment variable not set")
        elif not api_key.startswith("sk-ant-"):
            errors.append("ANTHROPIC_API_KEY has invalid format (should start with 'sk-ant-')")

        config["api_key_present"] = bool(api_key)
        config["api_key_valid_format"] = bool(api_key and api_key.startswith("sk-ant-"))

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

        Supports:
        - Bedrock (AWS)
        - Vertex (GCP)
        - Anthropic API Key (Docker/Server context)
        - Claude CLI OAuth (local development)
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

        elif self.auth_method == "anthropic_api":
            # Docker/Server context: Use ANTHROPIC_API_KEY directly
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if api_key:
                env_vars["ANTHROPIC_API_KEY"] = api_key
                logger.info("✅ Using ANTHROPIC_API_KEY for Claude Code SDK")

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