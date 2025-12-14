# Docker Wrapper - Aktueller Status

## ✅ Erfolgreich Abgeschlossen

1. **Lorenz's Arbeit gemerged** ✅
   - Branch `origin/feat/research-permissions-and-file-discovery` → main
   - Commit 51a7e37 (Docker Multi-Instance Setup)
   - .gitignore Konflikt aufgelöst

2. **Docker Installation** ✅
   - Docker Desktop installiert via Homebrew
   - Docker Daemon läuft
   - 3 Container Images gebaut (eco-wrapper-1, -2, -3)

3. **Container gestartet** ✅
   - eco-wrapper-1 (Port 8000) - UP
   - eco-wrapper-2 (Port 8010) - UP
   - eco-wrapper-3 (Port 8020) - UP

4. **Installationsskript erstellt** ✅
   - `install-docker-wrapper.sh` - Vollautomatische Installation
   - `DOCKER_QUICKSTART.md` - Dokumentation
   - `DOCKER_STATUS.md` - Status-Tracking (dieses Dokument)

## ⚠️ Noch zu erledigen

### OAuth Token Setup (KRITISCH)

Die Container laufen, aber scheitern am OAuth-Token Verification weil `secrets/claude_token.txt` einen Placeholder enthält.

**Problem**:
- OAuth Tokens werden vom Claude CLI sicher im System gespeichert
- Kein direkter Zugriff auf den Token aus laufender Session möglich
- Container brauchen eigenen Token für Authentication

**Lösung**:

```bash
# 1. In einem NEUEN Terminal-Fenster:
claude setup-token

# 2. Token kopieren und dann:
./setup-oauth-token.sh

# 3. Container neustarten:
cd docker && docker-compose restart

# 4. Validieren:
curl http://localhost:8000/health
```

## 🧪 Research Test

Nach dem Token-Setup kannst du die Research-Funktionalität testen:

```bash
# Test-Script ausführen:
./test-docker-research.sh
```

### Was das Script macht:
1. Prüft ob Container laufen
2. Testet Health Endpoint
3. Sendet `/sc:research` Request an wrapper-1
4. Zeigt Research-Ergebnisse an

### Manueller Test:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer test-key' \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "messages": [{
      "role": "user",
      "content": "/sc:research What are the latest AI developments?"
    }],
    "max_tokens": 4000
  }'
```

## 📊 Docker Architektur

```
eco-wrapper-1 (Port 8000) - wrapper-shared
├─ /app/logs           → wrapper-1-logs (volume)
├─ /app/instances      → wrapper-1-instances (volume)
├─ CLAUDE_CWD=/app/instances
└─ Session-Isolation via INSTANCE_NAME=eco-wrapper-1

eco-wrapper-2 (Port 8010) - wrapper-eco-backend
├─ /app/logs           → wrapper-2-logs (volume)
├─ /app/instances      → wrapper-2-instances (volume)
├─ CLAUDE_CWD=/app/instances
└─ Session-Isolation via INSTANCE_NAME=eco-wrapper-2

eco-wrapper-3 (Port 8020) - wrapper-eco-diagnostics
├─ /app/logs           → wrapper-3-logs (volume)
├─ /app/instances      → wrapper-3-instances (volume)
├─ CLAUDE_CWD=/app/instances
└─ Session-Isolation via INSTANCE_NAME=eco-wrapper-3
```

## 🔍 Debugging

### Container Logs anschauen:
```bash
docker logs eco-wrapper-1 --tail 50
docker logs eco-wrapper-2 --tail 50
docker logs eco-wrapper-3 --tail 50
```

### Container Status:
```bash
docker ps
```

### Health Checks:
```bash
curl http://localhost:8000/health  # wrapper-1
curl http://localhost:8010/health  # wrapper-2
curl http://localhost:8020/health  # wrapper-3
```

### Container neustarten:
```bash
cd docker && docker-compose restart
```

### Container stoppen:
```bash
cd docker && docker-compose down
```

### Container komplett neu bauen:
```bash
cd docker
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## 📝 Nächste Schritte

1. **Token Setup** (JETZT):
   ```bash
   claude setup-token     # In neuem Terminal
   ./setup-oauth-token.sh # Token eingeben
   ```

2. **Container neu starten**:
   ```bash
   cd docker && docker-compose restart
   ```

3. **Research testen**:
   ```bash
   ./test-docker-research.sh
   ```

4. **Bei Erfolg**:
   - Alle 3 Wrapper instances sind betriebsbereit
   - Isolierte Sessions für verschiedene Projekte
   - Research-Funktionalität verfügbar

## 🎯 Erwartetes Ergebnis

Nach erfolgreicher Token-Einrichtung:

```bash
$ curl http://localhost:8000/health
{"status": "healthy", "instance": "eco-wrapper-1"}

$ ./test-docker-research.sh
✅ Wrapper is healthy
✅ Research results: [AI research findings...]
```

## ❓ Troubleshooting

**Container startet nicht**: `docker logs eco-wrapper-1`
**Token invalid**: `docker logs eco-wrapper-1 | grep -i oauth`
**Port belegt**: `./stop-wrappers.sh` (stoppt normale Wrapper)
**Docker Daemon**: `open /Applications/Docker.app`

---

*Stand: 2025-11-08*
*Erstellt während Docker-Migration (Lorenz's Feat-Branch → Main)*
