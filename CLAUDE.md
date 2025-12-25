# AI-Bridge (Claude Code OpenAI Wrapper)

**Setup & Installation:** Siehe [README.md](README.md)

---

## Authentifizierung

### KRITISCH: Kein automatischer Fallback auf ANTHROPIC_API_KEY!

Diese Bridge verwendet **nur OAuth** für Claude CLI Aufrufe:

| Auth-Methode | Verwendung | Kosten |
|--------------|------------|--------|
| OAuth Token | Text-Requests (Chat, Research) | Kostenlos |
| ANTHROPIC_API_KEY | Nur Vision/Bilder | API-Kosten |
| Bedrock | Optional (CLAUDE_CODE_USE_BEDROCK=1) | AWS-Kosten |
| Vertex | Optional (CLAUDE_CODE_USE_VERTEX=1) | GCP-Kosten |

### Sicherheitsmechanismus

Beim Start passiert Folgendes automatisch (`src/auth.py`):

1. `ANTHROPIC_API_KEY` wird erkannt
2. Wird umbenannt zu `ANTHROPIC_VISION_API_KEY` (für Vision)
3. `ANTHROPIC_API_KEY` wird aus dem Environment **gelöscht**
4. Claude CLI sieht nur OAuth → kann nicht auf API-Key zurückfallen

**Garantie:** Wenn OAuth fehlschlägt → Request fehlschlägt. Keine versteckten API-Kosten!

### OAuth Token erneuern

```bash
# Lokal ausführen:
claude setup-token

# Token speichern:
echo 'DEIN_NEUES_TOKEN' > /path/to/bridge/secrets/claude_token.txt

# Auf Server synchronisieren:
rsync -avz secrets/ root@49.12.72.66:/root/werkingflow-bridge/secrets/

# Container neustarten:
ssh root@49.12.72.66 "cd /root/werkingflow-bridge/docker && docker compose restart"
```

Token ist 1 Jahr gültig.

---

## Hetzner Server (Production)

**URL:** `http://49.12.72.66:8000`
**SSH:** `ssh root@49.12.72.66`

### Features
- DSGVO-konforme Presidio-Anonymisierung (automatisch aktiv)
- 40 Minuten Timeout für Research-Tasks
- Automatischer Disk-Cleanup bei >80% Auslastung

### Server-Wartung

```bash
# Logs prüfen
ssh root@49.12.72.66 "docker logs eco-wrapper-universal --tail 50"

# Health-Check
curl http://49.12.72.66:8000/health

# Manueller Cleanup
ssh root@49.12.72.66 "docker builder prune -f && docker image prune -f"

# Rebuild mit automatischem Cleanup
ssh root@49.12.72.66 "cd /root/werkingflow-bridge/docker && ./rebuild.sh"
```

### Automatischer Disk-Cleanup

Cronjob läuft stündlich (`/etc/cron.hourly/docker-cleanup`):
- Prüft Disk-Auslastung
- Bei >80%: Docker Cache + alte Images werden gelöscht
- Log: `/var/log/docker-cleanup.log`

---

## Fehlerbehebung

### "Credit balance is too low"

**Problem:** OAuth Token ist abgelaufen oder ANTHROPIC_API_KEY wird fälschlicherweise verwendet.

**Lösung:**
1. Neues OAuth Token generieren: `claude setup-token`
2. Token auf Server synchronisieren
3. Container neustarten

### Container startet nicht

```bash
# Logs prüfen
docker logs eco-wrapper-universal

# Bei Disk-Problemen
docker system prune -a -f
docker builder prune -a -f
```

### SDK Verification Timeout

Die SDK-Verifikation dauert ~45-60 Sekunden beim Start. Das ist normal wegen:
- OAuth Token Validierung
- MCP Server Initialisierung
- spaCy Model Loading (Presidio)

---

## API Endpoints

| Endpoint | Methode | Beschreibung |
|----------|---------|--------------|
| `/v1/chat/completions` | POST | OpenAI-kompatibler Chat |
| `/v1/research` | POST | Research starten |
| `/v1/research/{session_id}/content` | GET | Research-Output downloaden |
| `/v1/models` | GET | Verfügbare Modelle |
| `/health` | GET | Health Check |

### Research Workflow

```bash
# 1. Research starten
curl -X POST "http://49.12.72.66:8000/v1/research" \
  -H "Authorization: Bearer test" \
  -H "Content-Type: application/json" \
  -d '{"query": "...", "depth": "deep"}'

# Response: {"session_id": "abc-123", ...}

# 2. Output downloaden
curl "http://49.12.72.66:8000/v1/research/abc-123/content" \
  -H "Authorization: Bearer test" \
  -o research_output.md
```

---

## Bedrock-Modus (DSGVO-konform)

Für volle DSGVO-Konformität kann die Bridge mit AWS Bedrock in eu-central-1 (Frankfurt) betrieben werden.

### Voraussetzungen

1. **AWS Bedrock Model Access** aktiviert (siehe `docs/manual-tasks/BEDROCK_SETUP.md`)
2. **AWS Credentials** in `.env.bedrock`

### Starten

```bash
# Mit Bedrock statt OAuth
docker compose -f docker-compose.yml -f docker-compose.bedrock.yml --env-file .env.bedrock up -d
```

### Dateien

| Datei | Beschreibung |
|-------|--------------|
| `docker/docker-compose.bedrock.yml` | Bedrock-Override |
| `docker/.env.bedrock` | AWS Credentials |
| `docs/manual-tasks/BEDROCK_SETUP.md` | Setup-Anleitung |

### Unterschiede zu OAuth

| Aspekt | OAuth | Bedrock |
|--------|-------|---------|
| Kosten | Kostenlos | AWS-Kosten (pay-per-token) |
| DSGVO | Presidio-Anonymisierung | Volle EU-Datenresidenz |
| Setup | OAuth Token | AWS Model Access |
| Region | US (Anthropic) | eu-central-1 (Frankfurt) |

---

## Lokale Entwicklung

```bash
cd /Users/rafael/Documents/GitHub/werkingflow/bridge

# Container starten
./start-wrappers.sh

# Container stoppen
./stop-wrappers.sh

# Logs
./logs.sh

# Health-Check
curl http://localhost:8000/health
```

---

## Wichtige Dateien

| Datei | Beschreibung |
|-------|--------------|
| `src/auth.py` | Authentifizierung, API-Key → Vision-Key Umbenennung |
| `src/claude_cli.py` | SDK-Integration, Fehlerbehandlung |
| `src/vision_provider.py` | Bild-Analyse (nutzt ANTHROPIC_VISION_API_KEY) |
| `docker/docker-compose.yml` | Container-Konfiguration |
| `docker/.env` | API-Keys (TAVILY, ANTHROPIC) |
| `secrets/claude_token.txt` | OAuth Token (1 Jahr gültig) |
| `docker/rebuild.sh` | Rebuild mit automatischem Cleanup |

---

*Letzte Aktualisierung: Dezember 2025*
