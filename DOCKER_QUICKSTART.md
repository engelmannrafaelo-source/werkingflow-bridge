# Docker Wrapper - Quick Start Guide

## 🚀 One-Command Installation

```bash
./install-docker-wrapper.sh
```

Das Script macht automatisch:
1. ✅ Docker Desktop installieren (via Homebrew)
2. ✅ Docker-Daemon starten & warten
3. ✅ Secrets einrichten (Claude Token aus ~/.claude.json)
4. ✅ Environment-Variablen (.env) vorbereiten
5. ✅ Docker-Images bauen
6. ✅ 3 Container starten (Ports 8000, 8010, 8020)
7. ✅ Health Checks prüfen
8. ✅ API-Test durchführen

---

## 📦 Manuelle Nutzung (nach Installation)

### Container starten
```bash
cd docker
docker-compose up -d
```

### Status prüfen
```bash
docker-compose ps
docker-compose logs -f wrapper-1
```

### Health Checks
```bash
curl http://localhost:8000/health  # wrapper-1
curl http://localhost:8010/health  # wrapper-2
curl http://localhost:8020/health  # wrapper-3
```

### API Test
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer test-key' \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 100
  }'
```

### Container stoppen
```bash
docker-compose down
```

### Container neu starten
```bash
docker-compose restart
```

### Logs anschauen
```bash
# Alle Logs
docker-compose logs -f

# Nur wrapper-1
docker-compose logs -f wrapper-1

# Nur wrapper-2
docker-compose logs -f wrapper-2

# Nur wrapper-3
docker-compose logs -f wrapper-3
```

---

## 🔧 Troubleshooting

### Docker-Daemon läuft nicht
```bash
open /Applications/Docker.app
# Warte bis Whale-Icon in Menüleiste erscheint
```

### Container starten nicht
```bash
# Logs prüfen
docker-compose logs

# Container neu bauen
docker-compose build --no-cache
docker-compose up -d
```

### Token-Probleme (WICHTIG!)
```bash
# Option 1: Long-Lived Token (EMPFOHLEN)
# 1. Öffne neues Terminal
# 2. Führe aus: claude setup-token
# 3. Kopiere den generierten Token
# 4. Führe aus: ./setup-oauth-token.sh

# Option 2: Manuell Token setzen
echo "dein-claude-oauth-token" > secrets/claude_token.txt
chmod 600 secrets/claude_token.txt
docker-compose restart

# Token validieren
docker logs eco-wrapper-1 | grep -i "oauth\|token\|auth"
```

### Port bereits belegt
```bash
# Alte Wrapper stoppen
../stop-wrappers.sh

# Oder Port-Konflikte prüfen
lsof -ti:8000
lsof -ti:8010
lsof -ti:8020
```

---

## 📊 Container-Architektur

```
eco-wrapper-1 (Port 8000)
├─ /app/logs           → wrapper-1-logs volume
├─ /app/instances      → wrapper-1-instances volume
└─ INSTANCE_NAME=eco-wrapper-1

eco-wrapper-2 (Port 8010)
├─ /app/logs           → wrapper-2-logs volume
├─ /app/instances      → wrapper-2-instances volume
└─ INSTANCE_NAME=eco-wrapper-2

eco-wrapper-3 (Port 8020)
├─ /app/logs           → wrapper-3-logs volume
├─ /app/instances      → wrapper-3-instances volume
└─ INSTANCE_NAME=eco-wrapper-3
```

Jeder Container hat:
- ✅ Eigene Logs (Named Volume)
- ✅ Eigene Instances/Sessions (Named Volume)
- ✅ Session-Isolation via CLAUDE_CWD
- ✅ Graceful Shutdown (15s stop_grace_period)
- ✅ Health Checks (30s interval)
- ✅ Auto-Restart (unless-stopped)

---

## 🎯 Unterschied zu normalen Wrappern

| Feature | Normal (./start-wrappers.sh) | Docker |
|---------|------------------------------|--------|
| **Installation** | Direkt auf System | Isoliert in Container |
| **Dependencies** | System Python/Poetry | Im Image gebaut |
| **Isolation** | Prozess-Level | Container-Level |
| **Logs** | `logs/` + `instances/*/logs/` | Named Volumes |
| **Persistenz** | Dateisystem | Docker Volumes |
| **Updates** | `git pull` + restart | Image rebuild |
| **Portabilität** | macOS-spezifisch | Überall wo Docker läuft |

---

## 📝 Nächste Schritte

1. **Installation durchführen:**
   ```bash
   ./install-docker-wrapper.sh
   ```

2. **Optional - Tavily API Key hinzufügen:**
   ```bash
   # Für /sc:research Funktionalität
   nano .env
   # TAVILY_API_KEY=tvly-your-key-here
   docker-compose restart
   ```

3. **Test durchführen:**
   ```bash
   curl http://localhost:8000/health
   ```

4. **Bei Problemen:**
   ```bash
   docker-compose logs -f
   ```
