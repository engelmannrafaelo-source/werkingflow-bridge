# AI-Bridge (Claude Code OpenAI Wrapper)

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
rsync -avz secrets/ root@95.217.180.242:/root/werkingflow-bridge/secrets/

# Container neustarten:
ssh root@95.217.180.242 "cd /root/werkingflow-bridge/docker && docker compose restart"
```

Token ist 1 Jahr gültig.

---

## Hetzner Server (Production)

**URL:** `http://95.217.180.242:8000`
**SSH:** `ssh root@95.217.180.242`

### Features
- DSGVO-konforme Presidio-Anonymisierung (automatisch aktiv)
- 40 Minuten Timeout für Research-Tasks
- Automatischer Disk-Cleanup bei >80% Auslastung

### Server-Wartung

```bash
# Logs prüfen
ssh root@95.217.180.242 "docker logs eco-wrapper-universal --tail 50"

# Health-Check
curl http://95.217.180.242:8000/health

# Manueller Cleanup
ssh root@95.217.180.242 "docker builder prune -f && docker image prune -f"

# Rebuild mit automatischem Cleanup
ssh root@95.217.180.242 "cd /root/werkingflow-bridge/docker && ./rebuild.sh"
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
