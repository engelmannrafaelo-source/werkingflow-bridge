#!/bin/bash
# =============================================================================
# EDEAIBridge Setup Script
# =============================================================================
# This script helps you configure EDEAIBridge for first-time use.
# It will:
#   1. Copy example configuration files
#   2. Guide you through API key setup
#   3. Verify the installation
# =============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  EDEAIBridge Setup${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Check if we're in the right directory
if [ ! -f "src/main.py" ]; then
    echo -e "${RED}Error: Please run this script from the EDEAIBridge root directory${NC}"
    exit 1
fi

# Step 1: Create .env from example
echo -e "${YELLOW}Step 1: Setting up environment configuration${NC}"
if [ -f ".env" ]; then
    echo -e "  ${GREEN}✓${NC} .env file already exists"
else
    cp .env.example .env
    echo -e "  ${GREEN}✓${NC} Created .env from .env.example"
fi

# Step 2: Create secrets directory structure
echo ""
echo -e "${YELLOW}Step 2: Setting up secrets directory${NC}"
mkdir -p secrets

if [ -f "secrets/claude_token.txt" ]; then
    echo -e "  ${GREEN}✓${NC} Claude token file exists"
else
    if [ -f "secrets/claude_token.txt.example" ]; then
        cp secrets/claude_token.txt.example secrets/claude_token.txt
    else
        echo "# Place your Claude OAuth token here" > secrets/claude_token.txt
    fi
    echo -e "  ${YELLOW}!${NC} Created secrets/claude_token.txt (needs your token)"
fi

if [ -f "secrets/hetzner_token.txt" ]; then
    echo -e "  ${GREEN}✓${NC} Hetzner token file exists"
else
    if [ -f "secrets/hetzner_token.txt.example" ]; then
        cp secrets/hetzner_token.txt.example secrets/hetzner_token.txt
    else
        echo "# Place your Hetzner API token here (optional)" > secrets/hetzner_token.txt
    fi
    echo -e "  ${YELLOW}!${NC} Created secrets/hetzner_token.txt (optional)"
fi

# Step 3: Check for Claude Code CLI
echo ""
echo -e "${YELLOW}Step 3: Checking prerequisites${NC}"

if command -v claude &> /dev/null; then
    echo -e "  ${GREEN}✓${NC} Claude Code CLI is installed"
else
    echo -e "  ${RED}✗${NC} Claude Code CLI not found"
    echo -e "    Install with: ${BLUE}npm install -g @anthropic-ai/claude-code${NC}"
fi

if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version 2>&1)
    echo -e "  ${GREEN}✓${NC} Python found: $PYTHON_VERSION"
else
    echo -e "  ${RED}✗${NC} Python3 not found"
fi

if command -v docker &> /dev/null; then
    echo -e "  ${GREEN}✓${NC} Docker is installed"
else
    echo -e "  ${YELLOW}!${NC} Docker not found (required for production deployment)"
fi

# Step 4: Configuration summary
echo ""
echo -e "${YELLOW}Step 4: Configuration Summary${NC}"
echo ""
echo -e "  ${BLUE}Required Configuration:${NC}"
echo ""
echo -e "  1. ${YELLOW}Tavily API Key${NC} (for /v1/research endpoint)"
echo -e "     Get your free key at: https://tavily.com"
echo -e "     Edit .env and set: TAVILY_API_KEY=tvly-your-key-here"
echo ""
echo -e "  2. ${YELLOW}Claude OAuth Token${NC} (for Claude AI access)"
echo -e "     Run: ${BLUE}claude login${NC}"
echo -e "     Then copy the token to: secrets/claude_token.txt"
echo ""
echo -e "  ${BLUE}Optional Configuration:${NC}"
echo ""
echo -e "  3. ${YELLOW}Hetzner API Token${NC} (for /v1/hetzner/* endpoints)"
echo -e "     Get at: https://console.hetzner.cloud"
echo -e "     Set in .env or secrets/hetzner_token.txt"
echo ""

# Step 5: Quick verification
echo -e "${YELLOW}Step 5: Quick Verification${NC}"
echo ""

# Check if .env has placeholder values
if grep -q "tvly-your-key-here" .env 2>/dev/null; then
    echo -e "  ${YELLOW}!${NC} TAVILY_API_KEY needs to be configured in .env"
else
    echo -e "  ${GREEN}✓${NC} TAVILY_API_KEY appears to be configured"
fi

# Check Claude token
if grep -q "YOUR_CLAUDE_OAUTH_TOKEN_HERE\|Place your\|^#" secrets/claude_token.txt 2>/dev/null; then
    echo -e "  ${YELLOW}!${NC} Claude OAuth token needs to be added to secrets/claude_token.txt"
else
    echo -e "  ${GREEN}✓${NC} Claude OAuth token appears to be configured"
fi

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}Setup complete!${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "Next steps:"
echo -e "  1. Configure your API keys as described above"
echo -e "  2. Run ${BLUE}./start.sh${NC} to start the service"
echo -e "  3. Test with ${BLUE}curl http://localhost:8000/health${NC}"
echo ""
echo -e "For more information, see README.md"
echo ""
