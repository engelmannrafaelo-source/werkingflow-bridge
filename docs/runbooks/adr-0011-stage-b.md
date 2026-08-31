# Runbook: ADR-0011 Stufe B — Föderation auf Prod scharf schalten

**Voraussetzung: Rafaels DIREKTE Freigabe in der ausführenden Session** (Prod-
Bridge-Deploy + Prod-Host-Konfig). Geschrieben 2026-08-31 (Session 7f122be0),
alle Host-Fakten an diesem Tag live gemessen; vor Ausführung die Pre-Checks
FRISCH wiederholen.

Reihenfolge ist wichtig: **Host-Konfig VOR dem Deploy** (der Deploy recreated
die Container und nimmt die Env dabei mit — zwei Neustarts gespart), und die
**Reaktivierung der ADR-0010-Default-Stufe erst NACH den Beweisen in beide
Richtungen** (Schritt 5).

Bekannte Falle aus Stufe A: compose liest die Interpolations-`.env` aus dem
**Compose-Datei-Verzeichnis** (`docker/.env`), NICHT aus dem Repo-Root.

---

## 0. Pre-Checks (read-only, < 2 min, am Tag der Ausführung frisch)

```bash
# Beide Bridges gesund?
curl -s http://49.12.72.66:8000/lb-status | head -c 200; echo
curl -s http://178.104.178.79:8000/lb-status | head -c 200; echo

# Principal-Drift (Cross-Bridge-Auth-Bedingung, s. ADR-0011 Punkt 2):
cd /root/projekte/werkingflow-bridge && bash scripts/check-principal-drift.sh || true

# Prod-Identität des LB (MUSS prod/49.12.72.66 sein — sonst stempelt prod
# Origin "dev" und die Buchhaltung läuft rückwärts):
ssh root@178.104.178.79 'docker exec wt-prod-lb sh -c "echo \$BRIDGE_ID / \$BRIDGE_BACKUP_HOST"'

# Foreign-Commit-Gate: nur eigene Commits seit letztem Prod-Deploy?
ssh root@178.104.178.79 'cat /root/werkingflow-bridge/.bridge-deployed-sha'
git log --oneline <deployed-sha>..origin/develop
```

Gemessen 31.08.: BRIDGE_ID=prod ✓, Backup=49.12.72.66 ✓ (⇒ geo-Trust nimmt
Dev-Hops an), Shared-Key beidseitig identisch ✓, 17/17 aktive Principals
byte-gleich ✓.

## 1. Prod-Host-Konfig (der eigentliche Prod-Eingriff, Rafael-gated)

```bash
ssh root@178.104.178.79
# a) Tailscale-Binding der prod platform-api (Tailscale-IP prod = 100.126.91.53):
grep -q ^PLATFORM_FEDERATION_BIND= /root/werkingflow-bridge/docker/.env || \
  echo "PLATFORM_FEDERATION_BIND=100.126.91.53" >> /root/werkingflow-bridge/docker/.env

# b) Origin-Identität + Peer "dev" für die Prod-Worker:
PENV=/root/werkingflow-bridge/secrets/platform.env
grep -q ^BRIDGE_ORIGIN_ID= $PENV || echo "BRIDGE_ORIGIN_ID=prod" >> $PENV
grep -q ^FEDERATION_PEERS= $PENV || cat >> $PENV <<'EOF'
FEDERATION_PEERS={"dev":{"platformUrl":"http://100.112.98.39:8300","tokenEnv":"FEDERATION_TOKEN_DEV"}}
EOF
# c) Token-Tausch: FEDERATION_TOKEN_DEV = BRIDGE_SERVICE_TOKEN der DEV-Bridge
#    (aus /root/werkingflow-bridge/secrets/platform.env auf 49.12.72.66 lesen,
#    NIE ins Repo/Chat — direkt Host-zu-Host übertragen):
#    ssh root@49.12.72.66 'grep ^BRIDGE_SERVICE_TOKEN= .../platform.env'  → Wert
#    echo "FEDERATION_TOKEN_DEV=<WERT>" >> $PENV
```

## 2. Dev-Host: echten Peer "prod" ergänzen (frei, kein Gate)

```bash
ssh root@49.12.72.66
PENV=/root/werkingflow-bridge/secrets/platform.env
# FEDERATION_PEERS um "prod" erweitern (test-Peer als Regressions-Sonde behalten):
# {"test":{...},"prod":{"platformUrl":"http://100.126.91.53:8300","tokenEnv":"FEDERATION_TOKEN_PROD"}}
# FEDERATION_TOKEN_PROD = BRIDGE_SERVICE_TOKEN der PROD-Bridge (Gegenrichtung von 1c).
# Danach Worker sequenziell recreaten (Kapazität schonen) ODER auf den
# nächsten Dev-Deploy warten — bis dahin ist der prod-Peer einfach unbenutzt.
```

## 3. Prod-Bridge-Deploy (Rafael-gated)

```bash
cd /root/projekte/werkingflow-bridge && ./scripts/bridge-deploy.sh server2
# Bringt: geteilte nginx.conf (Origin-Stempel + geo-Trust), Föderations-Code,
# gevendorte lua-resty-http. Auto-Rollback wie immer (ADR-0006).
# Danach: prod platform-api-Binding prüfen:
ssh root@178.104.178.79 'docker ps --format "{{.Names}}\t{{.Ports}}" | grep platform'
#   erwartet: 100.126.91.53:8300->8000
curl -s -o /dev/null -w '%{http_code}\n' http://100.126.91.53:8300/health   # von einem Tailnet-Node
```

## 4. Beweise in BEIDE Richtungen (je ~0,01–0,02 € Haiku-Call)

**Richtung A — dev-Origin, Prod führt aus** (der Reaktivierungs-Fall):
aus einem PROD-Worker-Container, direkt an einen Prod-Worker (Trust-Kette ist
seit Stufe A separat bewiesen):

```bash
ssh root@178.104.178.79 "docker exec wt-prod-worker-erk sh -c '
curl -s -w \"\nHTTP=%{http_code}\n\" -X POST http://localhost:8000/v1/chat/completions \
 -H \"Authorization: Bearer \$AI_BRIDGE_API_KEY\" -H \"Content-Type: application/json\" \
 -H \"X-App-ID: werking-report\" -H \"X-App-Env: local\" \
 -H \"X-User-ID: e127c1bd-edd1-404e-82b4-8fea764819eb\" \
 -H \"X-Bridge-Origin: dev\" \
 -d \"{\\\"model\\\":\\\"claude-3-5-haiku-20241022\\\",\\\"messages\\\":[{\\\"role\\\":\\\"user\\\",\\\"content\\\":\\\"Sag nur: ok\\\"}],\\\"max_tokens\\\":8}\"'"
# Erwartung: 200; prod-Worker-Log "federated to home bridge 'dev'" (volle Kette);
# DEV-DB: usage_events-Zeile + Deduct auf e127c1bd; PROD-DB: KEINE neue Zeile,
# KEIN neuer jit-User (SELECT count(*) FROM users WHERE email LIKE 'jit-%').
```

**Richtung B — prod-Origin, Dev führt aus** (Overflow-Fall): symmetrisch aus
einem Dev-Worker mit `X-Bridge-Origin: prod` und einem ECHTEN Prod-User mit
aktivem Budget (Demo-User wählen, z. B. david.engelmann@demo.werking.tools —
Prod-UUID vorher frisch nachschlagen). Erwartung spiegelverkehrt.

**Fail-closed-Gegenprobe** auf prod: Origin `test` (dort nicht konfiguriert)
→ 503 + Gate-Log „failing closed".

## 5. Reaktivierung ADR-0010-Default-Stufe (erst nach 4!)

```bash
# Ein-Zeilen-Revert des Pause-Commits im Generator, dann regenerieren:
git revert 31964d7   # bzw. die $llm_backend_pool-Zeile in generate-bridge-upstreams.sh
bash scripts/generate-bridge-upstreams.sh primary
git commit && git push && ./scripts/bridge-deploy.sh hetzner nginx
# Prod braucht KEINE Pool-Änderung (ADR-0010 Rollout-Abschnitt).
# Danach beobachten: access.jsonl llm_pool/origin, keine neuen jit-User,
# Budget-Bewegungen in der jeweils richtigen Heimat-DB.
```

## Rollback

- Schritt 5: Revert des Reverts + nginx-Deploy (auto-rollback-gesichert).
- Schritt 3: bridge-deploy rollt selbst zurück; manuell: voriges Manifest-Image.
- Schritt 1/2: Env-Zeilen entfernen + Container recreaten → Code ist ohne
  `BRIDGE_ORIGIN_ID` beweisbar inert (Stufe-A-Tests decken das ab).
