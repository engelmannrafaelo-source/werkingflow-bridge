#!/bin/bash
# Test Research Request to Docker Wrapper
# This demonstrates how to send a /sc:research request to the Docker-based wrapper

set -e

echo "=========================================="
echo "ECO OpenAI Wrapper - Research Test"
echo "=========================================="
echo ""

# Check if container is running
if ! docker ps | grep -q "eco-wrapper-universal"; then
    echo "❌ Docker container not running!"
    echo "Start it with: ./start-wrappers.sh"
    exit 1
fi

# Check health endpoint
echo "1. Checking wrapper health..."
HEALTH=$(curl -s http://localhost:8000/health || echo "FAILED")
if echo "$HEALTH" | grep -q "healthy"; then
    echo "✅ Wrapper is healthy"
else
    echo "❌ Wrapper is not healthy: $HEALTH"
    echo ""
    echo "Check logs: docker logs eco-wrapper-universal"
    exit 1
fi

echo ""
echo "2. Sending research request..."
echo "   Query: 'What are the latest developments in AI language models?'"
echo ""

# Send research request with SuperClaude /sc:research command
curl -X POST http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer test-key' \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "messages": [{
      "role": "user",
      "content": "/sc:research What are the latest developments in AI language models?"
    }],
    "max_tokens": 4000,
    "stream": false
  }' | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['choices'][0]['message']['content'])" 2>/dev/null || echo "❌ Research request failed"

echo ""
echo "=========================================="
echo "Test Complete!"
echo "=========================================="
echo ""
echo "If you see research results above, the Docker wrapper is working correctly!"
echo ""
echo "The universal wrapper is accessible on multiple ports:"
echo "  Port 8000: Primary endpoint"
echo "  Port 8010: eco-backend (backwards compatible)"
echo "  Port 8020: eco-diagnostics (backwards compatible)"
echo ""
