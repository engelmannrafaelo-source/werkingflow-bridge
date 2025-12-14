#!/bin/bash

# ECO OpenAI Wrapper - View Universal Container Logs

# Change to docker directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/docker"

# Get follow flag from argument (default: follow mode)
FOLLOW="${1:--f}"

echo "=========================================="
echo "ECO OpenAI Wrapper - Container Logs"
echo "=========================================="
echo ""

if [ "$FOLLOW" = "-f" ] || [ "$FOLLOW" = "--follow" ] || [ -z "$FOLLOW" ]; then
    echo "Following universal wrapper logs..."
    echo "Press Ctrl+C to stop"
    echo ""
    docker-compose logs -f eco-wrapper
elif [ "$FOLLOW" = "--tail" ] || [ "$FOLLOW" = "-t" ]; then
    echo "Last 100 lines of universal wrapper logs:"
    echo ""
    docker-compose logs --tail=100 eco-wrapper
else
    echo "Universal wrapper logs:"
    echo ""
    docker-compose logs eco-wrapper
fi
