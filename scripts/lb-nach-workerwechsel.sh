#!/usr/bin/env bash
# lb-nach-workerwechsel.sh — LB nach einem Wechsel von Worker-/platform-api-Containern
# neu laden UND pruefen, dass kein Ziel der Default-Pool-Anfragen ein Nicht-Worker ist.
#
# Hintergrund (11.11.2026): docker/upstreams-primary.conf loest worker1..4 beim Start
# von nginx EINMAL auf (statische upstream-Bloecke; OpenResty 1.27.1.1 kennt das
# `resolve` an `server` nicht, das kam erst mit nginx 1.27.3). Nach einem gleichzeitigen
# force-recreate aller Container zeigte eine alte Worker-IP auf platform-api: ~1/4 der
# Default-Pool-Anfragen bekam 404. Ein sequenzielles Rolling bekommt meist dieselben
# IPs zurueck und faellt nicht auf — verlassen darf man sich darauf nicht.
#
# Aufruf AUF DEM LB-HOST (bridge-deploy.sh ruft es per ssh im Repo-Checkout):
#   bash scripts/lb-nach-workerwechsel.sh [--nur-pruefen] [LB_CONTAINER] [WORKER_LISTE]
#   LB_CONTAINER  Standard wt-wrapper-lb
#   WORKER_LISTE  Standard "worker1 worker2 worker3 worker4" (worker_instance, wie /health meldet)
# Exit 0 = geladen (ausser --nur-pruefen) und 24 von 24 /health-Antworten 200 von einem
# erwarteten Worker, jeder Worker mindestens einmal; sonst 1 (Befund laut), 2 (Reload).
set -u
nur_pruefen=0
[[ "${1:-}" == "--nur-pruefen" ]] && { nur_pruefen=1; shift; }
lb="${1:-wt-wrapper-lb}"
workers="${2:-worker1 worker2 worker3 worker4}"
url="${LB_PRUEF_URL:-http://127.0.0.1:8000/health}"
n="${LB_PRUEF_ANZAHL:-24}"

if (( nur_pruefen == 0 )); then
    or=$(docker exec "$lb" sh -c 'command -v openresty || echo /usr/local/openresty/bin/openresty') || exit 2
    if ! tout=$(docker exec "$lb" "$or" -t -c /tmp/nginx.conf 2>&1); then
        echo "LB-FEHLER: openresty -t schlaegt fehl, kein Reload:"; printf '%s\n' "$tout" | tail -5; exit 2
    fi
    docker exec "$lb" "$or" -s reload -c /tmp/nginx.conf || { echo "LB-FEHLER: Reload fehlgeschlagen"; exit 2; }
    sleep 3
    echo "LB neu geladen ($lb)"
fi

messen() {
    declare -A seen=()
    bad=0
    for ((i = 0; i < n; i++)); do
        body=$(curl -s -m 10 -w '\n%{http_code}' "$url" 2>/dev/null) || body=$'\n000'
        code=${body##*$'\n'}
        json=${body%$'\n'*}
        wi=$(printf '%s' "$json" | python3 -c 'import sys,json; exec("try:\n print(json.load(sys.stdin).get(\"worker_instance\") or \"-\")\nexcept Exception:\n print(\"-\")")' 2>/dev/null)
        ok=0
        if [[ "$code" == "200" ]]; then
            for w in $workers; do [[ "$wi" == "$w" ]] && ok=1; done
        fi
        if (( ok )); then seen[$wi]=$(( ${seen[$wi]:-0} + 1 )); else bad=$((bad + 1)); seen["!${code}/${wi}"]=$(( ${seen["!${code}/${wi}"]:-0} + 1 )); fi
    done
    summary=""
    for k in "${!seen[@]}"; do summary+=" ${k}=${seen[$k]}"; done
    miss=""
    for w in $workers; do [[ -z "${seen[$w]:-}" ]] && miss+=" $w"; done
    if (( bad > 0 )) || [[ -n "$miss" ]]; then
        echo "LB-PRUEFUNG ROT: ${bad} von ${n} Antworten nicht von einem erwarteten Worker (Status/worker_instance:${summary}); nie getroffen:${miss:- -}"
        return 1
    fi
    echo "LB-PRUEFUNG GRUEN: ${n} von ${n} /health-Antworten 200 von erwarteten Workern (${summary# })"
}
messen; rc=$?
if (( rc != 0 )); then
    echo "erste Messung rot - eine Wiederholung nach 10 s (kein erneuter Reload)"
    sleep 10
    messen; rc=$?
fi
exit $rc
