# Docker Usage Guide

## Quick Start

### Build and Run
```bash
# Navigate to docker directory
cd docker

# Build and start all instances
docker compose up -d

# Check status
docker compose ps

# View logs
docker compose logs -f wrapper-1
```

### Stop and Cleanup
```bash
# Stop all instances (from docker/ directory)
docker compose down

# Stop and remove volumes
docker compose down -v
```

## Configuration

### Environment Variables

Create a `.env` file in the project root:
```bash
# Required
TAVILY_API_KEY=tvly-your-api-key-here

# Optional
LOG_LEVEL=INFO
MAX_TIMEOUT=2400000
```

### OAuth Token Setup

1. Create secrets directory in project root:
```bash
mkdir -p secrets
```

2. Save your Claude OAuth token:
```bash
echo "your-oauth-token-here" > secrets/claude_token.txt
```

3. Ensure proper permissions:
```bash
chmod 600 secrets/claude_token.txt
```

## Instance Management

### View Logs
```bash
# From docker/ directory
cd docker

# All instances
docker compose logs -f

# Specific instance
docker compose logs -f wrapper-1
docker compose logs -f wrapper-2
docker compose logs -f wrapper-3
```

### Restart Instance
```bash
# Restart single instance
docker compose restart wrapper-1

# Restart all
docker compose restart
```

### Execute Commands
```bash
# Access container shell
docker exec -it eco-wrapper-1 /bin/bash

# Check environment
docker exec eco-wrapper-1 printenv | grep TAVILY
```

## Troubleshooting

### Port Conflicts

If ports 8000, 8010, or 8020 are in use:
```bash
# Check what's using the port
lsof -i :8000

# Update docker-compose.yml to use different ports
```

### Volume Issues
```bash
# Remove and recreate volumes
docker compose down -v
docker compose up -d
```

### Health Check Failures
```bash
# Check health status
docker compose ps

# View detailed logs
docker compose logs wrapper-1 | tail -100
```

## Production Deployment

### Using Different Config
```bash
# Use production compose file
docker compose -f docker-compose.prod.yml up -d
```

### Updating
```bash
# Pull latest changes
cd /path/to/eco-openai-wrapper
git pull

# Rebuild and restart
cd docker
docker compose build
docker compose up -d
```

## Monitoring

### Resource Usage
```bash
# View resource usage
docker stats eco-wrapper-1 eco-wrapper-2 eco-wrapper-3
```

### Container Inspection
```bash
# Detailed container info
docker inspect eco-wrapper-1
```

---

**See also:**
- [MCP Setup](MCP_SETUP.md) - Configure MCP servers
- [Main README](../README.md) - General usage