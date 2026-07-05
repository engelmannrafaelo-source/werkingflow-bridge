#!/usr/bin/env bash
# spawn-infra-fix-session.sh — on a Bridge smoke failure, auto-spawn a CUI fix
# session so a broken infra endpoint is already being worked before anyone
# notices. Invoked by bridge-deploy.sh::phase_smoke_test on SMOKE_FAIL.
#
# This is the general pattern "infra error -> a session spawns itself"; the
# Bridge deploy smoke is its first consumer.
#
# Inputs (env): SMOKE_LABEL, SMOKE_URL, SMOKE_OUTPUT
# Best-effort: it must NEVER block or abort the deploy. The caller guards with
# `|| warn` and drives rollback from its own return code, not from this hook.
set -uo pipefail

CUI_API="${CUI_API:-http://localhost:4005}"
WORKDIR="${INFRA_FIX_WORKDIR:-/root/orchestrator/workspaces/devops}"
COOLDOWN_DIR="/tmp/bridge-infra-fix"
mkdir -p "$COOLDOWN_DIR"

label="${SMOKE_LABEL:-unknown}"
url="${SMOKE_URL:-?}"
out="${SMOKE_OUTPUT:-}"

fail_line="$(printf '%s\n' "$out" | grep -m1 'SMOKE_FAIL:' || true)"
if [[ -z "$fail_line" ]]; then
    echo "spawn-infra-fix: no SMOKE_FAIL line in output — nothing to spawn"
    exit 0
fi

# Cooldown: don't re-spawn for the same failing set within 30 min (retried deploys).
key="$(printf '%s' "$fail_line" | md5sum | cut -c1-12)"
marker="$COOLDOWN_DIR/$key"
if [[ -f "$marker" ]] && (( $(date +%s) - $(stat -c %Y "$marker") < 1800 )); then
    echo "spawn-infra-fix: cooldown active for this failure set — not re-spawning"
    exit 0
fi

# CUI reachable?
if ! curl -sf -m 5 "$CUI_API/api/mission/conversations" >/dev/null 2>&1; then
    echo "spawn-infra-fix: CUI API not reachable at $CUI_API — cannot spawn"
    exit 1
fi

# Rotate account to spread rate limits (gmail deliberately excluded — rate-limited).
accounts=(office engelmann werking)
ACCT="${accounts[$(( $(date +%s) % ${#accounts[@]} ))]}"

repro="$(printf '%s\n' "$out" | grep 'repro\[' || true)"
SUBJ="[Infra] Bridge smoke FAIL (${label}) — $(printf '%s' "$fail_line" | sed 's/SMOKE_FAIL: //; s/ probes failed:.*//')"

MSG="Der Bridge-Deploy-Smoke ist rot geworden — ein oder mehrere Bridge-Endpoints antworten in Produktion nicht mehr korrekt. Der Deploy hat deshalb automatisch zurueckgerollt (alter Stand laeuft weiter), aber der zugrundeliegende Defekt ist offen und du sollst ihn finden und sauber beheben.

Server/Profil: ${label} (${url})
Smoke-Ergebnis: ${fail_line}

Repro-Kommandos (so hat der Smoke getestet):
${repro}

Voller Smoke-Output:
${out}

Auftrag (deskriptiv): Schau dir die roten Endpoints an. Reproduziere den Fehler zuerst live gegen die Bridge (\$AI_BRIDGE_URL) und pruefe KRITISCH, ob es wirklich ein kaputter Endpoint ist ODER nur ein Smoke-Aufruf-Fehler (falscher Feldname / fehlender Header / falsches Request-Schema = KEIN Bug, dann gehoert der Fix in scripts/bridge_smoke.py, nicht in die App). Bei echtem Defekt: Wurzel finden — Bridge-Repo liegt lokal unter /root/projekte/werkingflow-bridge (Endpoint-Handler src/main.py; der privacy-pdf-/Docling-Service ist ein separates Image). Bring die Architektur in Ordnung. Der Deploy selbst (Hetzner-Image-Rebuild/Repoint) bleibt Rafaels Hand — du bereitest den Fix vor und meldest dich. Behebe so viel wie du sauber kannst; Reststellen dokumentieren statt mit Quickfix abhaken."

payload="$(ACCT="$ACCT" WORKDIR="$WORKDIR" SUBJ="$SUBJ" MSG="$MSG" python3 -c '
import json, os
print(json.dumps({
    "accountId": os.environ["ACCT"],
    "workDir": os.environ["WORKDIR"],
    "subject": os.environ["SUBJ"],
    "message": os.environ["MSG"],
    "model": "opus",
}))')"

resp="$(curl -sf -m 20 -X POST "$CUI_API/api/mission/start" \
    -H 'Content-Type: application/json' -d "$payload" 2>&1)" || {
    echo "spawn-infra-fix: mission/start failed: $resp"
    exit 1
}

touch "$marker"
echo "spawn-infra-fix: spawned fix session on account=${ACCT}: ${resp}"
exit 0
