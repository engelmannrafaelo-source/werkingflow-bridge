#!/bin/bash
set -e

echo "=========================================="
echo "ECO OpenAI Wrapper - Docker Installation"
echo "=========================================="
echo ""

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Step 1: Install Docker Desktop
echo -e "${BLUE}Step 1: Installing Docker Desktop...${NC}"
if ! command -v docker &> /dev/null; then
    echo "Docker not found. Installing via Homebrew..."
    brew install --cask docker
    echo -e "${GREEN}✅ Docker Desktop installed${NC}"
else
    echo -e "${GREEN}✅ Docker already installed${NC}"
fi

# Step 2: Start Docker Desktop
echo ""
echo -e "${BLUE}Step 2: Starting Docker Desktop...${NC}"
open -a Docker 2>/dev/null || echo "Docker app already running"

# Wait for Docker daemon
echo "Waiting for Docker daemon to start..."
for i in {1..30}; do
    if docker info &> /dev/null; then
        echo -e "${GREEN}✅ Docker daemon is running${NC}"
        break
    fi
    if [ $i -eq 30 ]; then
        echo -e "${RED}❌ Docker daemon failed to start within 30 seconds${NC}"
        echo "Please start Docker Desktop manually and run this script again"
        exit 1
    fi
    echo -n "."
    sleep 1
done

# Step 3: Setup Secrets
echo ""
echo -e "${BLUE}Step 3: Setting up secrets...${NC}"
mkdir -p secrets

# Get Claude token from ~/.claude.json
if [ -f ~/.claude.json ]; then
    CLAUDE_TOKEN=$(cat ~/.claude.json | python3 -c "import sys, json; print(json.load(sys.stdin).get('oauth_token', ''))" 2>/dev/null || echo "")

    if [ -n "$CLAUDE_TOKEN" ] && [ "$CLAUDE_TOKEN" != "null" ]; then
        echo "$CLAUDE_TOKEN" > secrets/claude_token.txt
        chmod 600 secrets/claude_token.txt
        echo -e "${GREEN}✅ Claude token saved to secrets/claude_token.txt${NC}"
    else
        echo -e "${YELLOW}⚠️  No Claude token found in ~/.claude.json${NC}"
        echo "Please add your token manually to secrets/claude_token.txt"
        exit 1
    fi
else
    echo -e "${RED}❌ ~/.claude.json not found${NC}"
    echo "Please create secrets/claude_token.txt with your Claude OAuth token"
    exit 1
fi

# Step 4: Setup Environment
echo ""
echo -e "${BLUE}Step 4: Setting up environment...${NC}"
if [ ! -f .env ]; then
    cp .env.example .env
    echo -e "${GREEN}✅ .env file created from template${NC}"
    echo -e "${YELLOW}⚠️  Please edit .env and add your TAVILY_API_KEY if needed${NC}"
else
    echo -e "${GREEN}✅ .env file already exists${NC}"
fi

# Step 5: Build Docker Images
echo ""
echo -e "${BLUE}Step 5: Building Docker images...${NC}"
cd docker
docker-compose build
echo -e "${GREEN}✅ Docker images built${NC}"

# Step 6: Start Containers
echo ""
echo -e "${BLUE}Step 6: Starting Docker containers...${NC}"
docker-compose up -d
echo -e "${GREEN}✅ Containers started${NC}"

# Step 7: Wait for Health Checks
echo ""
echo -e "${BLUE}Step 7: Waiting for services to become healthy...${NC}"
sleep 10

# Check health endpoints (all ports point to same container)
echo "Checking universal wrapper (port 8000)..."
for i in {1..10}; do
    if curl -sf http://localhost:8000/health &> /dev/null; then
        echo -e "${GREEN}✅ Universal wrapper is healthy (port 8000)${NC}"
        break
    fi
    [ $i -eq 10 ] && echo -e "${RED}❌ Universal wrapper failed to start${NC}"
    sleep 2
done

echo "Verifying port 8010 (backward compatible)..."
if curl -sf http://localhost:8010/health &> /dev/null; then
    echo -e "${GREEN}✅ Port 8010 accessible (eco-backend alias)${NC}"
else
    echo -e "${RED}❌ Port 8010 not accessible${NC}"
fi

echo "Verifying port 8020 (backward compatible)..."
if curl -sf http://localhost:8020/health &> /dev/null; then
    echo -e "${GREEN}✅ Port 8020 accessible (eco-diagnostics alias)${NC}"
else
    echo -e "${RED}❌ Port 8020 not accessible${NC}"
fi

# Step 8: Test API
echo ""
echo -e "${BLUE}Step 8: Testing API endpoint...${NC}"
RESPONSE=$(curl -s -X POST http://localhost:8000/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -H 'Authorization: Bearer test-key' \
    -d '{
        "model": "claude-3-5-sonnet-20241022",
        "messages": [{"role": "user", "content": "Say only: Docker Wrapper funktioniert!"}],
        "max_tokens": 20
    }' | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['choices'][0]['message']['content'])" 2>/dev/null || echo "API test failed")

if [ -n "$RESPONSE" ]; then
    echo -e "${GREEN}✅ API Response: ${RESPONSE}${NC}"
else
    echo -e "${YELLOW}⚠️  API test inconclusive, check logs:${NC}"
    echo "docker-compose logs eco-wrapper"
fi

# Summary
echo ""
echo "=========================================="
echo -e "${GREEN}Installation Complete!${NC}"
echo "=========================================="
echo ""
echo "Container Status:"
docker-compose ps
echo ""
echo "Universal Wrapper Endpoints (all point to same container):"
echo "  Port 8000: http://localhost:8000/health (primary)"
echo "  Port 8010: http://localhost:8010/health (eco-backend alias)"
echo "  Port 8020: http://localhost:8020/health (eco-diagnostics alias)"
echo ""
echo "Management Commands:"
echo "  View logs:    cd .. && ./logs.sh"
echo "  Stop:         cd .. && ./stop-wrappers.sh"
echo "  Restart:      cd .. && ./restart-wrappers.sh"
echo ""
echo "Memory Usage: Limited to 4 GB (efficient universal container)"
echo ""
