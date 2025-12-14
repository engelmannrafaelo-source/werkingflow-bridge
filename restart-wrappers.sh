#!/bin/bash

# ECO OpenAI Wrapper - Restart Universal Docker Container

# Change to docker directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/docker"

echo "=========================================="
echo "ECO OpenAI Wrapper - Restarting Container"
echo "=========================================="
echo ""

# Restart container
echo "Restarting universal wrapper container..."
docker-compose restart

if [ $? -ne 0 ]; then
    echo ""
    echo "❌ Failed to restart Docker container"
    echo "    Try: ./stop-wrappers.sh && ./start-wrappers.sh"
    exit 1
fi

echo ""
echo "Waiting for container to restart..."
sleep 5

echo ""
echo "Container Status:"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
echo "Running health checks..."
echo ""

# Health check function
health_check() {
    local port=$1
    local name=$2
    local max_attempts=30
    local attempt=0

    while [ $attempt -lt $max_attempts ]; do
        if curl -s -f "http://localhost:$port/health" > /dev/null 2>&1; then
            echo "✅ $name (port $port) - healthy"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done

    echo "⚠️  $name (port $port) - not responding after ${max_attempts}s"
    return 1
}

# Health check all port aliases
health_check 8000 "Universal Wrapper (primary)"
health_check 8010 "Universal Wrapper (eco-backend alias)"
health_check 8020 "Universal Wrapper (eco-diagnostics alias)"

echo ""
echo "✅ Universal wrapper container restarted successfully"
echo ""
