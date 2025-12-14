#!/bin/bash

# ECO OpenAI Wrapper - Stop Universal Docker Container

# Change to docker directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/docker"

echo "=========================================="
echo "ECO OpenAI Wrapper - Stopping Container"
echo "=========================================="
echo ""

# Stop container
echo "Stopping universal wrapper container..."
docker-compose down

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Universal wrapper container stopped successfully"
else
    echo ""
    echo "⚠️  Container may not have stopped cleanly"
    echo "    Run 'docker ps' to check for running containers"
fi

echo ""
