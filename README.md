# AI-Bridge (werkingflow)

**OpenAI-kompatibler Claude Code Wrapper**

FastAPI-basierter Wrapper für Claude AI mit OpenAI-kompatiblen Endpoints. DSGVO-konform mit Presidio-Anonymisierung.

## Production Server

**Hetzner**: `http://49.12.72.66:8000`

```bash
# Health Check
curl http://49.12.72.66:8000/health

# Privacy/Presidio Status
curl http://49.12.72.66:8000/v1/privacy/status
```

## 🎯 Purpose

This service acts as a centralized API gateway that:
- Translates OpenAI API format to Claude AI format
- Provides authentication and rate limiting
- Handles session management for long-running requests
- Offers robust restart and timeout handling
- Serves both `eco-backend` (report generation) and `eco-diagnostics` (frontend)

## 🏗️ Architecture Position

```
eco-diagnostics (Frontend) ──┐
                             ├─→ eco-openai-wrapper (Universal Container)
eco-backend (Pipeline)    ───┘     ↓
                                  Accessible on Ports 8000, 8010, 8020
                                  (All point to same container)
```

## 🚀 Docker Quick Start

**Production-ready Docker setup with auto-start and isolated instances.**

### Prerequisites

- Docker Desktop (macOS/Windows) or Docker Engine (Linux)
- Claude OAuth Token (free, no API costs)
- Tavily API Key (for research functionality)

### Installation

```bash
# 1. Clone the repository
cd ~/Documents/GitHub
git clone https://github.com/ecoenergygroup-rengelmann/eco-openai-wrapper.git
cd eco-openai-wrapper

# 2. Run automated Docker installation
./install-docker-wrapper.sh
```

The installation script will:
- ✅ Check Docker Desktop installation
- ✅ Setup Claude OAuth token (prompts you to run `claude setup-token`)
- ✅ Configure environment variables (Tavily API key)
- ✅ Build Docker images
- ✅ Start containers with health checks
- ✅ Verify all endpoints are responding

### Manual Configuration (if needed)

If you skip the installer script:

1. **Setup Claude OAuth Token**:
```bash
# Install Claude CLI if not installed
npm install -g @anthropic-ai/claude-code

# Authenticate via OAuth (one-time, free)
claude setup-token

# Copy token to secrets file
# Token will be displayed, copy it to:
echo "sk-ant-oat01-..." > secrets/claude_token.txt
```

2. **Configure Tavily API Key**:
```bash
# Get your Tavily API key from: https://tavily.com
# Add to .env file:
echo "TAVILY_API_KEY=tvly-your-key-here" > .env
```

### Start the Service

```bash
# Start all 3 Docker containers
./start-wrappers.sh
```

The service will start a **single universal wrapper** accessible on multiple ports:
- **Port 8000**: Primary endpoint - http://localhost:8000
- **Port 8010**: eco-backend (backwards compatible) - http://localhost:8010
- **Port 8020**: eco-diagnostics (backwards compatible) - http://localhost:8020

**All ports point to the same container** - this architecture provides:
- ✅ **66% less RAM usage** (1 container instead of 3)
- ✅ **Memory limit: 4 GB** (down from ~8 GB unbounded usage)
- ✅ **Backwards compatibility** - existing code works without changes

**Auto-Start**: Container automatically restarts on system boot via `restart: unless-stopped` policy.

### Management Commands

```bash
# View container logs (follow mode)
./logs.sh

# View last 100 lines of logs
./logs.sh --tail

# Restart container (e.g., after code changes)
./restart-wrappers.sh

# Stop container
./stop-wrappers.sh

# Check container status
docker ps

# Health checks (all ports work)
curl http://localhost:8000/health  # Primary
curl http://localhost:8010/health  # eco-backend alias
curl http://localhost:8020/health  # eco-diagnostics alias
```

### Available Endpoints

The universal wrapper provides (accessible on all 3 ports):
- **Chat Completions**: `/v1/chat/completions` (OpenAI-compatible)
- **Research Endpoint**: `/v1/research` (SuperClaude research with depth/strategy options)
- **Health Check**: `/health`
- **API Documentation**: `/docs`

### Research Output

All research reports are saved to:
- **Host**: `~/eco-research-output/`
- **Container**: `/app/research_output/` (mounted to host)

## 🔧 Configuration

### Environment Variables

```env
# Optional
PORT=8000                        # Server port (default: 8000)
HOST=0.0.0.0                     # Bind address (default: 0.0.0.0)
LOG_LEVEL=info                   # Logging level
```

**IMPORTANT**: Authentication is handled via Claude CLI OAuth (`claude login`). Do NOT set ANTHROPIC_API_KEY.

### Timeout Settings

The service is configured with generous timeouts for long-running LLM requests:
- **Keep-alive timeout**: 300 seconds (5 minutes)
- **Graceful shutdown**: 30 seconds

## 📡 API Usage

### OpenAI-Compatible Format

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy-key" \
  -d '{
    "model": "claude-sonnet-4",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ],
    "max_tokens": 1000
  }'
```

### Supported Models

- `claude-sonnet-4` / `claude-3-5-sonnet-20241022` (Default)
- `claude-opus-4` / `claude-opus-4-20250514`
- `gpt-4` (mapped to Claude Sonnet)
- `gpt-3.5-turbo` (mapped to Claude Sonnet)

### Streaming Support

```python
import requests

response = requests.post(
    "http://localhost:8000/v1/chat/completions",
    json={
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "Write a story"}],
        "stream": True
    },
    stream=True
)

for line in response.iter_lines():
    if line:
        print(line.decode('utf-8'))
```

## 🛡️ Features

### Robust Restart Handling

The `start_wrapper.sh` script includes:
- Automatic port cleanup (kills existing processes)
- Health check before startup
- Graceful shutdown on Ctrl+C
- Error recovery and logging

### Rate Limiting

Built-in rate limiting to prevent API abuse:
- Request-based limiting
- Token-based limiting
- Configurable per-client limits

### Session Management

Long-running request support with:
- Session tracking
- Timeout management
- Connection keep-alive

### Authentication

Simple API key authentication:
- Header-based: `Authorization: Bearer <key>`
- Query parameter: `?api_key=<key>`

## 🔗 Integration

### With eco-backend

The backend uses this wrapper for:
- Report generation (Phase 9-11)
- Web research (Phase 2, 6)
- Data analysis (Phase 3, 5, 7)

Configuration in `eco-backend/.env`:
```env
WRAPPER_URL=http://localhost:8000/v1
WRAPPER_API_KEY=dummy-key
```

### With eco-diagnostics

The frontend can optionally use this for:
- AI-powered insights
- Report preview generation
- Data validation assistance

## 📁 Project Structure

```
eco-openai-wrapper/
├── main.py                 # FastAPI application
├── auth.py                # Authentication logic
├── rate_limiter.py        # Rate limiting
├── session_manager.py     # Session handling
├── models.py              # Data models
├── parameter_validator.py # Request validation
├── message_adapter.py     # OpenAI ↔ Claude translation
├── start_wrapper.sh       # Robust start script
├── pyproject.toml         # Poetry configuration
├── .env.example           # Environment template
└── examples/              # Usage examples
```

## 🧪 Testing

```bash
# Run basic health check
curl http://localhost:8000/health

# Test completion endpoint
python examples/test_basic.py

# Run full test suite
poetry run pytest
```

## 🐛 Troubleshooting

### Port Already in Use

The start script automatically cleans up port 8000, but if issues persist:

```bash
# Manual port cleanup
lsof -i :8000 -t | xargs kill -9
```

### Poetry Not Found

```bash
# Install Poetry
curl -sSL https://install.python-poetry.org | python3 -

# Add to PATH
export PATH="/Users/$USER/.local/bin:$PATH"
```

### Connection Timeout

Increase timeout in your client:
```python
response = requests.post(url, json=data, timeout=300)  # 5 minutes
```

## 📝 Logs

Logs are written to:
- **Console**: Real-time server output
- **server.log**: Detailed request/response logs
- **claude_wrapper.log**: Claude API interactions

## 🔄 Updates

```bash
# Pull latest changes
cd ~/Documents/GitHub/eco-openai-wrapper
git pull

# Update dependencies
poetry install
```

## 🤝 Team Collaboration

For colleagues setting up the system:

1. **Clone all repositories**:
```bash
cd ~/Documents/GitHub
git clone https://github.com/ecoenergygroup-rengelmann/eco-openai-wrapper.git
git clone https://github.com/ecoenergygroup-rengelmann/eco-backend.git
git clone https://github.com/ecoenergygroup-rengelmann/eco-projects.git
git clone https://github.com/ecoenergygroup-rengelmann/eco-diagnostics.git
```

2. **Configure API keys**: Add your Claude API key to `.env`

3. **Start the wrapper**: Run `./start_wrapper.sh`

4. **Use from backend/frontend**: Services will connect to `http://localhost:8000`

## 📚 Related Documentation

- [eco-backend](../eco-backend/README.md) - Backend pipeline system
- [eco-diagnostics](../eco-diagnostics/README.md) - Frontend application
- [eco-projects](../eco-projects/README.md) - Project data repository

## 📄 License

Proprietary - ECO Energy Group

---

**Maintained by**: Rafael Rengelmann
**Organization**: Werkingflow
**Last Updated**: December 2025
