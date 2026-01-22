#!/bin/bash
# =============================================================================
# Multi-Worker Bridge Launcher
# 3 Container mit je eigenem Token + nginx Load Balancer
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  Multi-Worker Bridge (3 Container + Load Balancer)${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Check token files
echo -e "\n${YELLOW}Checking tokens...${NC}"
TOKENS_OK=true

check_token() {
    local file=$1
    local name=$2
    if [[ -f "$file" ]] && [[ -s "$file" ]]; then
        local preview=$(head -c 30 "$file")
        echo -e "  ${GREEN}✓${NC} $name: ${preview}..."
    else
        echo -e "  ${RED}✗${NC} $name: MISSING or EMPTY"
        TOKENS_OK=false
    fi
}

check_token "secrets/claude_token.txt" "Token 1 (Primary)"
check_token "secrets/claude_token_account1.txt" "Token 2 (Account 1)"
check_token "secrets/claude_token_account2.txt" "Token 3 (Account 2)"

if [[ "$TOKENS_OK" != "true" ]]; then
    echo -e "\n${RED}ERROR: Missing tokens! Add tokens to secrets/ folder.${NC}"
    exit 1
fi

# Stop existing containers
echo -e "\n${YELLOW}Stopping existing containers...${NC}"
docker compose -f docker/docker-compose.multi.yml down 2>/dev/null || true
docker compose -f docker/docker-compose.yml down 2>/dev/null || true

# Build and start
echo -e "\n${YELLOW}Building and starting multi-worker setup...${NC}"
docker compose -f docker/docker-compose.multi.yml up -d --build

# Wait for health
echo -e "\n${YELLOW}Waiting for workers to be healthy...${NC}"
sleep 10

# Check status
echo -e "\n${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Status${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

docker compose -f docker/docker-compose.multi.yml ps

echo -e "\n${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Endpoints${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "  API:        http://localhost:8000/v1/chat/completions"
echo -e "  Health:     http://localhost:8000/health"
echo -e "  LB Status:  http://localhost:8000/lb-status"
echo -e ""
echo -e "  ${BLUE}Load Balancing: Round-Robin über 3 Worker${NC}"
echo -e "  ${BLUE}Jeder Request geht automatisch zum nächsten Worker${NC}"
echo -e ""

# Test health
echo -e "${YELLOW}Testing endpoints...${NC}"
if curl -sf http://localhost:8000/health > /dev/null; then
    echo -e "  ${GREEN}✓${NC} Health check OK"
else
    echo -e "  ${RED}✗${NC} Health check FAILED"
fi

if curl -sf http://localhost:8000/lb-status > /dev/null; then
    LB_STATUS=$(curl -s http://localhost:8000/lb-status)
    echo -e "  ${GREEN}✓${NC} Load Balancer: $LB_STATUS"
else
    echo -e "  ${RED}✗${NC} Load Balancer FAILED"
fi

echo -e "\n${GREEN}Done!${NC} Use 'docker compose -f docker/docker-compose.multi.yml logs -f' to watch logs."
