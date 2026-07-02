# AI-Bridge (Claude Code OpenAI Wrapper)

**Setup & Installation:** Siehe [README.md](README.md)
**Architektur-Entscheidungen:** Siehe [docs/adr/](docs/adr/) — insb. **ADR-0006** (Single-Source)

---

## KRITISCH FÜR CLAUDE-SESSIONS

Die Bridge ist **Live-Produktions-Infrastruktur** (bedient alle Apps + CUI-Sessions + Tester,
tausende Calls/Tag). Nicht autonom „reparieren".

**ABSOLUTE REGELN:**

1. **NIE Configs am Host von Hand editieren.** Kein `vim nginx.conf` auf dem Hetzner-Host,
   kein manuelles Ändern von Container-Files. Der frühere „edit on Hetzner → capture drift
   back"-Loop war die Ursache des Energy-Phase-4-Incidents (Prod lieferte ein anderes Schema
   als Dev). Änderungen laufen **ausschließlich** über Repo → `scripts/bridge-deploy.sh`.
2. **NIE ohne explizite Anweisung an der Bridge arbeiten.** Nicht Container manuell
   starten/stoppen, nicht Token-Dateien überschreiben, nicht „Logs analysieren um zu fixen".
3. Bei 5xx: **warten** (nginx macht automatisch Retry/Failover). Bei Token-Problemen:
   **User informieren** (manuelles Token-Update nötig, siehe unten).
4. Deploy nur mit `bridge-deploy.sh` aus committed ref, und **nur mit User-Freigabe** —
   Prod bedient zahlende Kunden.

---

## Zwei Bridges, EIN Repo — Single-Source (ADR-0006)

Es gibt **zwei** Bridge-Deployments, beide gebaut aus **diesem einen Repo** (`werkingflow-bridge`):

| | DEV / Primary (`hetzner`) | PROD (`server2`) |
|---|---|---|
| Host | `49.12.72.66` | `178.104.178.79` |
| Zweck | interne Dev/Test-Last, CUI-Sessions, Tester | zahlende Kunden |
| Worker-Set | `worker1..4` | `worker-sahori`, `worker-kurt` |
| nginx-LB Container | `wt-wrapper-lb` | `wt-prod-lb` |
| Privacy | **lokaler** Container (`wt-privacy-pdf-service`) | **remote** über Tailscale (`http://100.112.98.39:8100`), kein lokaler Container |
| Compose-Files | `docker-compose.yml` + `docker-compose-platform-overlay.yml` | `docker-compose-prod.yml` + `docker-compose-prod-platform.yml` |
| Repo-Pfad **am Host** | `/root/werkingflow-bridge` | `/root/werkingflow-bridge` |

**Das Prinzip:** dev und prod unterscheiden sich **nur** durch den generierten Upstreams-Include
(= Worker-Set + Backup-Host). Alles andere ist per Konstruktion identisch:

- **`docker/nginx.conf`** = die *eine* geteilte nginx-Quelle (OpenResty+Lua). Enthält keine
  Worker-Namen. `docker/nginx-prod.conf` wurde **gelöscht** (war der Drift-Herd).
- **`docker/lua/pool_router.lua`** ist topologie-agnostisch: liest `WORKER_NAMES` aus der
  `BRIDGE_WORKERS`-env und das Account→Worker-Mapping **live** aus `account-pool-state`
  (kein hardcodierter Map mehr).
- **Der einzige per-Host-Unterschied** kommt aus `scripts/generate-bridge-upstreams.sh` →
  `docker/upstreams-{primary,prod}.conf` (per Compose als `/etc/nginx/upstreams.conf` gemountet).
- Beide nginx-LB bauen aus **`docker/Dockerfile.nginx-lb`** (OpenResty+Lua) — auch Prod hat
  jetzt den intelligenten Lua-Pool-Router.

**Akzeptanzkriterium (live verifizierbar):** `GET /v1/metrics/account-pool-state` liefert auf
**beiden** Hosts dasselbe Aggregat-Schema `{"ts":…, "accounts":{…}}` — nur der Account-Satz
unterscheidet sich (dev 4 / prod 2). Ein flaches Single-Worker-Objekt = Regression.

### Deploy — nur aus dem Repo, nie am Host

```bash
# Deploy (pullt develop am Host, baut, recreated Container, Smoke+Rollback-gated):
scripts/bridge-deploy.sh <hetzner|server2|both> [service...] [--dry-run]

# Drift-Detektor — MUSS 4/4 grün sein (currency + keine modifizierten tracked Files +
# In-Container-Includes sha256-identisch zum Repo + Upstreams-Include korrekt):
scripts/bridge-parity-check.sh <hetzner|server2>
```

`bridge-deploy.sh` validiert die geteilte `nginx.conf` für **beide** Topologien
(`openresty -t` mit gemountetem Upstreams-Include), läuft Smoke- + Distribution-Tests und
**rollt bei Fehler automatisch zurück**. Prod bekommt zusätzlich `X-Priority: production` im
Smoke-Test. Prod darf hinter dev liegen (= gewollter, bekannter Zustand: „noch nicht deployed"),
aber nie durch stillen Hand-Drift.

**Prod-Cutover-Regel:** Prod nicht deployen, während eine Energy-Pipeline auf Prod läuft.

---

## Multi-Worker Architektur

Jede Bridge = **N Worker + metrics-reader (Aggregator) + nginx-LB** (Round-Robin bzw.
Lua-Pool-Router). Der Worker-Satz ist per-Host und steht in `docker/upstreams-{primary,prod}.conf`
(SSoT) — **nicht** hier hardcoden. Welcher Account gerade welchem Worker zugeordnet ist, steht
live in `/v1/metrics/account-pool-state` / `/lb-status`.

### Container-Übersicht

**DEV / Primary (`49.12.72.66`):**

| Container | Funktion |
|-----------|----------|
| `wt-wrapper-lb` | nginx Load Balancer (OpenResty+Lua) |
| `wt-wrapper-worker1..4` | 4 Worker |
| `wt-wrapper-metrics-reader` | Pool-State-Aggregator |
| `wt-platform-api` | Platform-API (usage/timeseries/auth/users/…) |
| `bridge-postgres-prod` | Platform-/Identity-DB |
| `wt-privacy-pdf-service` | lokale Presidio/Docling-Anonymisierung |

**PROD (`178.104.178.79`):**

| Container | Funktion |
|-----------|----------|
| `wt-prod-lb` | nginx Load Balancer (OpenResty+Lua) |
| `wt-prod-worker-sahori`, `wt-prod-worker-kurt` | 2 Worker |
| `wt-prod-metrics-reader` | Pool-State-Aggregator |
| `wt-prod-platform-api` | Platform-API |
| `bridge-postgres-prod` | Platform-/Identity-DB |
| _(kein lokaler Privacy-Container)_ | nutzt Remote-Privacy der Dev-Bridge über Tailscale |

---

## Authentifizierung

### Token-Architektur (Einjahres-Tokens)

**Es gibt KEIN Auto-Refresh!** Tokens werden manuell gesetzt und sind 1 Jahr gültig.
Sie liegen **host-lokal** und flach unter `secrets/claude_token_*.txt` (nicht im Repo).

| Host | Token-Dateien |
|------|---------------|
| DEV `49.12.72.66` | `secrets/claude_token_account1..4.txt` (+ Namens-Symlinks engelmann/gmail/office/werking(flow)) |
| PROD `178.104.178.79` | `secrets/claude_token_worker-sahori.txt`, `secrets/claude_token_worker-kurt.txt` |

### Neues Token setzen (nach Ablauf / Wechsel)

```bash
# Beispiel DEV, Account 1:
ssh root@49.12.72.66 "echo 'sk-ant-oat01-...' > /root/werkingflow-bridge/secrets/claude_token_account1.txt"
# Danach die Worker neu ausrollen (aus dem Repo, gated):
scripts/bridge-deploy.sh hetzner   # bzw. server2 für Prod-Tokens

# Beispiel PROD, Worker sahori:
ssh root@178.104.178.79 "echo 'sk-ant-oat01-...' > /root/werkingflow-bridge/secrets/claude_token_worker-sahori.txt"
```

> Nur das Token-File ist host-lokal (Secret). Die Container-/nginx-Config kommt aus dem Repo —
> nach Token-Änderung reicht ein Redeploy/Restart, **kein** Hand-Edit an Compose/nginx.

---

## Status prüfen (read-only, jederzeit erlaubt)

```bash
# Health + Worker-Info (pro Host):
curl http://49.12.72.66:8000/health
curl http://178.104.178.79:8000/health

# Load-Balancer-Status (Accounts, Worker):
curl http://49.12.72.66:8000/lb-status

# Pool-State (Aggregat-Schema muss auf BEIDEN Hosts gleich sein):
curl -H "Authorization: Bearer $AI_BRIDGE_API_KEY" http://49.12.72.66:8000/v1/metrics/account-pool-state
curl -H "Authorization: Bearer $AI_BRIDGE_API_KEY" http://178.104.178.79:8000/v1/metrics/account-pool-state

# Drift-Check gegen das Repo (siehe oben):
scripts/bridge-parity-check.sh hetzner
scripts/bridge-parity-check.sh server2

# Container-Status:
ssh root@49.12.72.66 "docker ps"
```

### Features
- DSGVO-konforme Presidio-Anonymisierung (automatisch aktiv)
- Bis 40 Minuten Timeout für Research-Tasks
- Load Balancing über mehrere Accounts (Lua-Pool-Router wählt frischesten Account)

---

## API Endpoints

| Endpoint | Methode | Beschreibung |
|----------|---------|--------------|
| `/v1/chat/completions` | POST | OpenAI-kompatibler Chat |
| `/v1/research` | POST | Research starten |
| `/v1/research/{session_id}/content` | GET | Research-Output downloaden |
| `/v1/models` | GET | Verfügbare Modelle |
| `/v1/metrics/account-pool-state` | GET | Aggregierter Pool-Zustand (schema-kritisch, s.o.) |
| `/health` | GET | Health Check + Worker-Info |
| `/lb-status` | GET | Load Balancer Status |

### Research Workflow

```bash
# 1. Research starten
curl -X POST "http://49.12.72.66:8000/v1/research" \
  -H "Authorization: Bearer $AI_BRIDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "...", "depth": "deep"}'
# Response: {"session_id": "abc-123", ...}

# 2. Output downloaden
curl "http://49.12.72.66:8000/v1/research/abc-123/content" \
  -H "Authorization: Bearer $AI_BRIDGE_API_KEY" \
  -o research_output.md
```

---

## Fehlerbehebung

### "OAuth token has expired"
Einjahres-Token abgelaufen → neues Token setzen (siehe Authentifizierung) + Redeploy.

### 503 Service Unavailable
Worker temporär weg. nginx macht automatisch Failover. Bei persistenten 503s Token prüfen.
**Nicht** am Host herumfixen.

### Schema-Mismatch bei `account-pool-state` (flach statt aggregate)
= nginx-Drift (die ADR-0006-Fehlerklasse). Prüfen mit `bridge-parity-check.sh <host>`;
Fix = sauberer Redeploy aus dem Repo, **nie** Hand-Edit am Host.

### Container startet nicht / Disk-Probleme
```bash
ssh root@49.12.72.66 "docker compose -f /root/werkingflow-bridge/docker/docker-compose.yml logs"
ssh root@49.12.72.66 "docker system prune -a -f"   # bei Disk-Druck
```

---

## Wichtige Dateien

| Datei | Beschreibung |
|-------|--------------|
| `docker/nginx.conf` | **Die eine geteilte** LB-Config (OpenResty+Lua), beide Bridges |
| `docker/upstreams-{primary,prod}.conf` | einziger per-Host-Unterschied (Worker-Set) |
| `scripts/generate-bridge-upstreams.sh` | generiert die Upstreams-Includes |
| `docker/lua/pool_router.lua` | topologie-agnostischer Account-Pool-Router |
| `docker/Dockerfile.nginx-lb` | nginx-Image (OpenResty+Lua) für beide Bridges |
| `docker/docker-compose.yml` (+`-platform-overlay.yml`) | DEV-Stack |
| `docker/docker-compose-prod.yml` (+`-prod-platform.yml`) | PROD-Stack |
| `scripts/bridge-deploy.sh` | **einziger** Deploy-Pfad (gated, Rollback) |
| `scripts/bridge-parity-check.sh` | Drift-/Currency-/Parity-Check |
| `docs/adr/0006-*.md` | SSoT für das Single-Source-Modell |
| `secrets/claude_token_*.txt` | Token-Dateien (host-lokal, nicht im Repo) |
| `src/auth.py`, `src/claude_cli.py` | Auth + SDK-Integration |

---

## Automatische Wartung

| Cron | Script | Funktion |
|------|--------|----------|
| `0 3 * * *` | `daily_cleanup.sh` | Tägliche Docker + Log Bereinigung |
| `0 * * * *` | `disk_check.sh` | Stündlich: Cleanup wenn Disk >70% |

**Logs-Retention:** Logs 1 Tag · Research-Output 7 Tage

---

*Letzte Aktualisierung: 2026-07-02 (ADR-0006 Single-Source-Cutover verifiziert)*
