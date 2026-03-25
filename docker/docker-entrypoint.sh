#!/bin/bash
set -e

# Fix permissions for volume directories at runtime
# This must run as root before dropping to claude user
chown -R claude:claude /app/logs /app/instances

# Load OAuth token from file and write to a persistent env file
# The SDK subprocess needs CLAUDE_CODE_OAUTH_TOKEN at spawn time
if [ -n "$CLAUDE_CODE_OAUTH_TOKEN_FILE" ] && [ -f "$CLAUDE_CODE_OAUTH_TOKEN_FILE" ]; then
    TOKEN=$(cat "$CLAUDE_CODE_OAUTH_TOKEN_FILE")
    # Remove old token file if exists (may have wrong permissions after restart)
    rm -f /tmp/claude_token 2>/dev/null || true
    # Write to a file that can be sourced or read
    echo "$TOKEN" > /tmp/claude_token
    chmod 644 /tmp/claude_token
    chown claude:claude /tmp/claude_token
    # Also export for this shell
    export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"
    echo "✅ Loaded OAuth token from $CLAUDE_CODE_OAUTH_TOKEN_FILE (${TOKEN:0:30}...)"
    echo "   Token saved to /tmp/claude_token for subprocess inheritance"
fi

# Graceful shutdown handler
shutdown_handler() {
    echo "🛑 Graceful shutdown initiated..."

    # Get uvicorn PID (running as claude user)
    UVICORN_PID=$(pgrep -u claude -f "uvicorn.*main:app" || true)

    if [ -n "$UVICORN_PID" ]; then
        echo "📡 Sending SIGTERM to uvicorn (PID: $UVICORN_PID)..."
        kill -TERM "$UVICORN_PID" 2>/dev/null || true

        # Wait up to 10 seconds for graceful shutdown
        for i in {1..10}; do
            if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
                echo "✅ Uvicorn stopped gracefully"
                break
            fi
            sleep 1
        done

        # Force kill if still running
        if kill -0 "$UVICORN_PID" 2>/dev/null; then
            echo "⚠️  Force killing uvicorn..."
            kill -KILL "$UVICORN_PID" 2>/dev/null || true
        fi
    fi

    echo "✅ Shutdown complete"
    exit 0
}

# Trap SIGTERM and SIGINT (sent by docker stop)
trap shutdown_handler SIGTERM SIGINT

# CRITICAL: Remove ANTHROPIC_API_KEY from environment to prevent SDK fallback
# Vision uses ANTHROPIC_VISION_API_KEY (set in docker-compose.yml directly)
# If ANTHROPIC_API_KEY leaks in, the SDK silently falls back to paid API!
if [ -n "$ANTHROPIC_API_KEY" ]; then
    echo "⚠️  ANTHROPIC_API_KEY detected in environment - REMOVING to prevent SDK fallback!"
    echo "   Vision key should be set via ANTHROPIC_VISION_API_KEY instead."
    unset ANTHROPIC_API_KEY
fi

# Drop privileges and execute CMD as claude user in background
# Pass CLAUDE_CODE_OAUTH_TOKEN explicitly via env command
# IMPORTANT: Do NOT pass ANTHROPIC_API_KEY - OAuth only!
if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
    gosu claude env CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" "$@" &
else
    gosu claude "$@" &
fi

# Store child PID
CHILD_PID=$!

# Wait for child process (uvicorn)
wait $CHILD_PID