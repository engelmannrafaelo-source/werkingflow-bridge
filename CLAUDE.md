# AI-Bridge (Claude Code OpenAI Wrapper)

**Setup & Installation:** Siehe [README.md](README.md)

---

## KRITISCH FÜR CLAUDE-SESSIONS

**NIEMALS die Bridge modifizieren, debuggen oder "reparieren"!**

Die Bridge ist ein **isoliertes System**. Bei Problemen:

1. **NICHT** Docker-Container manuell starten/stoppen
2. **NICHT** Logs analysieren um "Probleme zu fixen"
3. **NICHT** Code in `/root/projekte/werkingflow/bridge/` ändern
4. **NICHT** Token-Dateien ändern oder überschreiben

**Was stattdessen tun:**
- Bei 5xx Errors: **Warten** (automatischer Retry durch nginx)
- Bei Token-Problemen: **User informieren** (manuelles Token-Update nötig)

**ABSOLUTE REGEL: Arbeite NIEMALS selbstständig an der Bridge ohne explizite Anweisung vom User!**

---

## Multi-Worker Architektur

Die Bridge läuft mit **2 Workern + nginx Load Balancer**:

```
┌─────────────────────────────────────────────────────────────┐
│  nginx Load Balancer (Port 8000)                            │
│  Round-Robin Strategy                                       │
├─────────────────────────────────────────────────────────────┤
│  Worker 1                    │  Worker 2                    │
│  rafael@engelmann.at         │  office@data-energyneering   │
│  claude_token_account1.txt   │  claude_token_account3.txt   │
└─────────────────────────────────────────────────────────────┘
```

### Container-Übersicht

| Container | Funktion |
|-----------|----------|
| `eco-wrapper-lb` | nginx Load Balancer |
| `eco-wrapper-worker1` | Worker mit Account 1 Token |
| `eco-wrapper-worker2` | Worker mit Account 3 Token |

### KRITISCH: Nur Multi-Worker Config verwenden!

```bash
# RICHTIG - Multi-Worker Setup
docker compose -f docker-compose.multi.yml up -d

# FALSCH - Alte Single-Container Config
# docker-compose.yml ist disabled
```

---

## Authentifizierung

### Token-Architektur (Einjahres-Tokens)

**Es gibt KEIN Auto-Refresh!** Tokens werden manuell gesetzt und sind 1 Jahr gültig.

| Worker | Account | Token-Datei |
|--------|---------|-------------|
| Worker 1 | `rafael@engelmann.at` | `claude_token_account1.txt` |
| Worker 2 | `office@data-energyneering.at` | `claude_token_account3.txt` |
| (PAUSED) | `engelmann.rafaelo` | `claude_token_account2.txt` |

### Token-Dateien auf Hetzner

```
/root/werkingflow-bridge/secrets/
├── claude_token_account1.txt    # Einjahres-Token Account 1
├── claude_token_account3.txt    # Einjahres-Token Account 3
├── claude_token_account2.txt    # PAUSED
└── hetzner_token.txt            # Hetzner API Token
```

### Neues Token setzen

```bash
# Token für Account 1 setzen
ssh root@49.12.72.66 "echo 'sk-ant-oat01-...' > /root/werkingflow-bridge/secrets/claude_token_account1.txt"

# Token für Account 3 setzen
ssh root@49.12.72.66 "echo 'sk-ant-oat01-...' > /root/werkingflow-bridge/secrets/claude_token_account3.txt"

# Worker neustarten damit neue Tokens geladen werden
ssh root@49.12.72.66 "cd /root/werkingflow-bridge/docker && docker compose -f docker-compose.multi.yml restart"
```

---

## Hetzner Server (Production)

**URL:** `http://49.12.72.66:8000`
**SSH:** `ssh root@49.12.72.66`

### Status prüfen

```bash
# Health + Worker-Info
curl http://49.12.72.66:8000/health

# Load Balancer Status (Accounts, Workers)
curl http://49.12.72.66:8000/lb-status

# Container-Status
ssh root@49.12.72.66 "docker ps"
```

### Server-Wartung

```bash
# Logs prüfen
ssh root@49.12.72.66 "docker compose -f /root/werkingflow-bridge/docker/docker-compose.multi.yml logs -f"

# Neustarten
ssh root@49.12.72.66 "cd /root/werkingflow-bridge/docker && docker compose -f docker-compose.multi.yml restart"

# Rebuild
ssh root@49.12.72.66 "cd /root/werkingflow-bridge/docker && docker compose -f docker-compose.multi.yml up -d --build"
```

### Features
- DSGVO-konforme Presidio-Anonymisierung (automatisch aktiv)
- 40 Minuten Timeout für Research-Tasks
- Round-Robin Load Balancing (doppelte Rate-Limits)

---

## API Endpoints

| Endpoint | Methode | Beschreibung |
|----------|---------|--------------|
| `/v1/chat/completions` | POST | OpenAI-kompatibler Chat |
| `/v1/research` | POST | Research starten |
| `/v1/research/{session_id}/content` | GET | Research-Output downloaden |
| `/v1/models` | GET | Verfügbare Modelle |
| `/health` | GET | Health Check + Worker-Info |
| `/lb-status` | GET | Load Balancer Status |

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

## Fehlerbehebung

### "OAuth token has expired"

**Problem:** Einjahres-Token ist abgelaufen.

**Lösung:** Neues Token generieren und setzen:
```bash
ssh root@49.12.72.66 "echo 'NEUES_TOKEN' > /root/werkingflow-bridge/secrets/claude_token_accountX.txt"
ssh root@49.12.72.66 "cd /root/werkingflow-bridge/docker && docker compose -f docker-compose.multi.yml restart"
```

### 503 Service Unavailable

**Problem:** Worker temporär nicht verfügbar.

**Lösung:** nginx macht automatisch Failover zum anderen Worker. Bei persistenten 503s Token prüfen.

### Container startet nicht

```bash
# Logs prüfen
ssh root@49.12.72.66 "docker compose -f /root/werkingflow-bridge/docker/docker-compose.multi.yml logs"

# Bei Disk-Problemen
ssh root@49.12.72.66 "docker system prune -a -f"
```

---

## Lokale Entwicklung

```bash
cd /root/projekte/werkingflow/bridge

# Container starten (Multi-Worker)
./start-wrappers.sh

# Container stoppen
./stop-wrappers.sh

# Logs
./logs.sh

# Health-Check
curl http://localhost:8000/health
curl http://localhost:8000/lb-status
```

---

## Wichtige Dateien

| Datei | Beschreibung |
|-------|--------------|
| `docker/docker-compose.multi.yml` | Multi-Worker Config (VERWENDEN!) |
| `docker/nginx.conf` | Load Balancer Config |
| `secrets/claude_token_account1.txt` | Token Account 1 |
| `secrets/claude_token_account3.txt` | Token Account 3 |
| `src/auth.py` | Authentifizierung |
| `src/claude_cli.py` | SDK-Integration |

---

## Dritten Account reaktivieren

Wenn `engelmann.rafaelo` wieder aktiv ist:

1. Neues Token setzen:
   ```bash
   ssh root@49.12.72.66 "echo 'NEUES_TOKEN' > /root/werkingflow-bridge/secrets/claude_token_account2.txt"
   ```

2. In `docker-compose.multi.yml` worker3 uncomment

3. In `nginx.conf` worker3 uncomment

4. Neustarten:
   ```bash
   ssh root@49.12.72.66 "cd /root/werkingflow-bridge/docker && docker compose -f docker-compose.multi.yml up -d --build"
   ```

---

## Automatische Wartung

| Cron | Script | Funktion |
|------|--------|----------|
| `0 3 * * *` | `daily_cleanup.sh` | Tägliche Docker + Log Bereinigung |
| `0 * * * *` | `disk_check.sh` | Stündlich: Cleanup wenn Disk >70% |

### Logs-Retention

- **Logs**: 1 Tag
- **Research Output**: 7 Tage

---

*Letzte Aktualisierung: Januar 2026*
