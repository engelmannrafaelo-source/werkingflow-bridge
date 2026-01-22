#!/bin/bash
# ECO OpenAI Wrapper - Start Multi-Worker Setup (2 Workers + Load Balancer)
# Round-robin between rafael@engelmann.at and office@data-energyneering.at

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/docker"

echo "==========================================="
echo "ECO Bridge - Starting Multi-Worker Setup"
echo "==========================================="
echo ""

# Start multi-worker setup
echo "Starting 2 workers + nginx load balancer..."
docker compose -f docker-compose.multi.yml up -d

if [ $? -ne 0 ]; then
    echo ""
    echo "❌ Failed to start containers"
    exit 1
fi

echo ""
echo "Waiting for containers to start..."
sleep 10

echo ""
echo "Container Status:"
docker ps --format "table {{.Names}}\t{{.Status}}"

echo ""
echo "==========================================="
echo "Multi-Worker Bridge Ready"
echo "==========================================="
echo ""
echo "Load Balancer: http://localhost:8000"
echo "Workers: worker1 (rafael@engelmann.at), worker2 (office@data-energyneering.at)"
echo "Strategy: Round-Robin"
echo ""
echo "Status: curl http://localhost:8000/lb-status"
echo ""
