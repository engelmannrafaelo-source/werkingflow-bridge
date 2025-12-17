#!/bin/bash
# Rebuild script with automatic cleanup to prevent disk space issues

set -e

echo "🔧 Stopping container..."
docker compose down

echo "🏗️  Building new image..."
docker compose build

echo "🧹 Cleaning up build cache..."
docker builder prune -f

echo "🚀 Starting container..."
docker compose up -d

echo "✅ Done! Checking disk space..."
df -h /
