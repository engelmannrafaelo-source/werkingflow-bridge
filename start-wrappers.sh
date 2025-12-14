#!/bin/bash

# ECO OpenAI Wrapper - Start Universal Docker Container
# Single container accessible on multiple ports

# Change to docker directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/docker"

echo "=========================================="
echo "ECO OpenAI Wrapper - Starting Container"
echo "=========================================="
echo ""

# Start container
echo "Starting universal wrapper container..."
docker-compose up -d

if [ $? -ne 0 ]; then
    echo ""
    echo "❌ Failed to start Docker container"
    echo ""
    echo "Troubleshooting:"
    echo "  1. Check Docker Desktop is running"
    echo "  2. Check secrets/claude_token.txt exists"
    echo "  3. Check .env file has TAVILY_API_KEY"
    echo "  4. Run: docker-compose logs"
    exit 1
fi

echo ""
echo "Waiting for container to start..."
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
echo "=========================================="
echo "Universal Wrapper Ready"
echo "=========================================="
echo ""
echo "API Endpoints (all point to same container):"
echo "  http://localhost:8000  - Primary endpoint"
echo "  http://localhost:8010  - eco-backend (backwards compatible)"
echo "  http://localhost:8020  - eco-diagnostics (backwards compatible)"
echo ""
echo "Research Output:"
echo "  ~/eco-research-output/"
echo ""
echo "Management:"
echo "  View logs:    ./logs.sh"
echo "  Restart:      ./restart-wrappers.sh"
echo "  Stop:         ./stop-wrappers.sh"
echo ""
echo "Memory Usage: Limited to 4 GB (down from ~8 GB with 3 containers)"
echo ""
