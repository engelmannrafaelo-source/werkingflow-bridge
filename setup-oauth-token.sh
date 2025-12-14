#!/bin/bash
# Setup OAuth Token for Docker Wrapper
# This script helps extract the OAuth token for Docker containers

echo "=========================================="
echo "ECO OpenAI Wrapper - OAuth Token Setup"
echo "=========================================="
echo ""

echo "Option 1: Generate Long-Lived Token (RECOMMENDED)"
echo "------------------------------------------------"
echo "Run this command in a new terminal window:"
echo ""
echo "  claude setup-token"
echo ""
echo "This will guide you through creating a long-lived authentication token."
echo "Copy the generated token and paste it when prompted below."
echo ""
read -p "Have you generated the token? (y/n): " has_token

if [ "$has_token" = "y" ] || [ "$has_token" = "Y" ]; then
    echo ""
    read -sp "Paste your Claude OAuth token: " token
    echo ""

    if [ -n "$token" ]; then
        echo "$token" > secrets/claude_token.txt
        chmod 600 secrets/claude_token.txt
        echo ""
        echo "✅ Token saved to secrets/claude_token.txt"
        echo ""
        echo "Now restart the Docker containers:"
        echo "  cd docker && docker-compose restart"
    else
        echo "❌ No token provided"
        exit 1
    fi
else
    echo ""
    echo "Please run 'claude setup-token' first, then run this script again."
    exit 1
fi

echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
echo "Test the wrapper:"
echo "  curl http://localhost:8000/health"
echo ""
