# AI-Bridge - Project Context for Claude

## Repository

**Location**: `/Users/rafael/Documents/GitHub/ai-bridge`
**Server**: `http://95.217.180.242:8000` (Hetzner Production)

## Purpose

OpenAI-kompatibler API-Wrapper für Claude Code SDK mit:
- DSGVO-konformer Presidio-Anonymisierung
- Vision/Bild-Analyse Support
- Research Endpoint für tiefgehende Analysen
- Session-Management

## Key Endpoints

| Endpoint | Beschreibung |
|----------|--------------|
| `/v1/chat/completions` | OpenAI-kompatibel (Haupt-Endpoint) |
| `/v1/research` | Deep Research mit Hops |
| `/v1/models` | Verfügbare Modelle |
| `/v1/privacy/status` | Presidio-Status |
| `/health` | Health Check |

## Quick Commands

### Lokal testen
```bash
cd /Users/rafael/Documents/GitHub/ai-bridge
./start-wrappers.sh
curl http://localhost:8000/health
```

### Auf Hetzner deployen
```bash
# 1. Lokal committen und pushen
git add -A && git commit -m "fix: beschreibung" && git push

# 2. SSH zu Hetzner und updaten
ssh root@95.217.180.242
cd /root/ai-bridge && git pull && docker-compose down && docker-compose build --no-cache && docker-compose up -d
```

## Architecture

```
src/
├── main.py              # FastAPI Endpoints
├── claude_cli.py        # Claude Code SDK Integration
├── models.py            # Pydantic Models (OpenAI-kompatibel)
├── vision_provider.py   # Bild-Analyse (URL + Base64)
├── message_adapter.py   # Format-Konvertierung
└── privacy/
    └── middleware.py    # Presidio PII-Anonymisierung
```

## Important Files

- `docker/docker-compose.yml` - Container-Konfiguration
- `docker/.env` - Environment (ANTHROPIC_API_KEY, TAVILY_API_KEY)
- `DEPLOYMENT.md` - Deployment-Anleitung inkl. Quick-Update

---

*Last Updated: December 2025*
