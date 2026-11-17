# shellcheck shell=bash
# Log-Sicherung vor jedem Container-Recreate (Plan V2.1 §11 (ii)/(iii), Kanzler-Wort 17.11.2026).
#
# Warum: Docker-Logs (json-file) liegen im Container-Ordner und verschwinden
# mit jedem --force-recreate. Abgleich F (Pool) und jede Nachfrage zu einem
# Lauf brauchen sie aber laenger als bis zum naechsten Deploy.
#
# Was: log_sichern <host> <container> kopiert <LogPath> und alle rotierten
# <LogPath>.N UNVERAENDERT nach
#   ${LOG_SICHERUNG_BASIS}/<container>/<UTC>-<container-id12>/roh/
# prueft jede Kopie gegen die Quelle (sha256 ueber die kopierte Laenge — die
# aktive Datei waechst weiter, der Kopierstand ist ihr Praefix), scannt den
# Ordner mit gitleaks (gepinntes Image, --network none, --redact), schreibt
# MANIFEST und packt dann jede Datei mit gzip -9 (Rueckprobe zcat|sha256).
#
# Vertrag:
#   * Nur auf dem Dev-Host (HETZNER_HOST). Andere Hosts: Hinweis, rc 0.
#   * rc 0 = gesichert und belegt, oder Container existiert nicht (nichts zu sichern).
#   * rc !=0 = Sicherung NICHT belegt. Der Deploy erzeugt den Dienst dann
#     nicht neu; der Rollback warnt laut und macht weiter (er muss reparieren).
#   * Ohne Scanner (Image fehlt / falsche ID / Scannerfehler) kein Erfolg.
#   * Ein gitleaks-FUND haelt nicht an (die Logs sind schon geschrieben):
#     laute Warnung mit Regel und Datei:Zeile, nie ein Wert; Vermerk im MANIFEST.
#   * Liest nur Log-Dateien und Metadaten (LogPath, Image, Label) — nie Env.
#   * Loescht nichts. Ab LOG_SICHERUNG_WARN_GB Gesamtgroesse: Warnung.

# Scanner: dasselbe Image, mit dem c9a50d3b am 16./17.11.2026 gescannt hat
# (gitleaks v8.24.3, ANTWORT-c9a50d3b-GITLEAKS-WEG 1ccde74805b7). Aufruf NUR per
# Digest, nie per Tag; auf dem Host per `docker pull <digest>` geholt (Kanzler-Wort
# 17.11.2026). Kein anderes Image, keine Logs vom Host weg.
LOG_SICHERUNG_GITLEAKS="${LOG_SICHERUNG_GITLEAKS:-ghcr.io/gitleaks/gitleaks@sha256:e1b35e12a8c6fa8901f060459cfb6b2fc4c484d3afbe3b029733a3bbfab07055}"
LOG_SICHERUNG_BASIS="${LOG_SICHERUNG_BASIS:-${REMOTE_REPO}/log-sicherung}"
LOG_SICHERUNG_WARN_GB="${LOG_SICHERUNG_WARN_GB:-20}"
# Exit-Code, den gitleaks bei einem Fund liefert (alles andere ausser 0 = Scannerfehler).
LOG_SICHERUNG_FUND_RC=3
# rc von deploy_one_service, wenn NUR die Log-Sicherung scheiterte (Zweitpruefung L R1, L-1):
# der Dienst wurde nicht neu erzeugt und darf auch im Rollback nicht neu erzeugt werden.
LOG_SICHERUNG_RC=4

# Lokaler Halte-Tag fuer den Scanner (Fenster 17.11.2026, 08:09Z): ein nur per Digest gezogenes Image
# hat keinen Tag und gilt fuer `docker image prune -f` (phase_preflight) als "dangling" -> der Deploy
# loeschte seinen eigenen Scanner, die Vorpruefung brach ab. Der Tag zeigt auf DASSELBE Image (kein Pull,
# kein neues Image); aufgerufen und geprueft wird weiter nur per Digest.
LOG_SICHERUNG_GITLEAKS_HALT="${LOG_SICHERUNG_GITLEAKS_HALT:-bridge-log-sicherung/gitleaks:digest-halt}"

# Vor jedem Image-Prune auf dem Dev-Host: Scanner anheften, falls vorhanden. Fehlt er, aendert sich
# nichts; dann bricht die Vorpruefung (Phase 3.8) laut ab.
log_sicherung_scanner_anheften() {
    local host="$1"
    if [[ "$host" != "$HETZNER_HOST" ]]; then
        return 0
    fi
    rssh "$host" "docker image inspect '${LOG_SICHERUNG_GITLEAKS}' >/dev/null 2>&1 && docker tag '${LOG_SICHERUNG_GITLEAKS}' '${LOG_SICHERUNG_GITLEAKS_HALT}'" >/dev/null 2>&1 || true
    return 0
}

# Vorpruefung EINMAL je Host vor Phase 4 (L-1 (b)): Scanner per Digest vorhanden, RepoDigest gleich.
# Scheitert sie, ist noch kein Container angefasst; deploy_server setzt nur den Code zurueck.
log_sicherung_vorpruefung() {
    local host="$1"
    if [[ "$host" != "$HETZNER_HOST" ]]; then
        return 0
    fi
    if [[ "$LOG_SICHERUNG_GITLEAKS" != *@sha256:* ]]; then
        error_ "Log-Sicherung: Scanner ohne Digest (${LOG_SICHERUNG_GITLEAKS})"
        return 1
    fi
    if [[ "$DRY_RUN" == "true" ]]; then
        info "[DRY-RUN] Log-Sicherung-Vorpruefung: Scanner ${LOG_SICHERUNG_GITLEAKS} auf ${host}"
        return 0
    fi
    local digests
    digests=$(rssh "$host" "docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' '${LOG_SICHERUNG_GITLEAKS}' 2>/dev/null") || digests=""
    if ! grep -qxF "$LOG_SICHERUNG_GITLEAKS" <<< "$digests"; then
        error_ "Log-Sicherung-Vorpruefung: Scanner ${LOG_SICHERUNG_GITLEAKS} fehlt auf ${host} oder RepoDigest abweichend — kein Recreate"
        return 1
    fi
    info "Log-Sicherung-Vorpruefung: Scanner per Digest auf ${host} vorhanden"
    return 0
}

log_sichern() {
    local host="$1"
    local container="$2"
    if [[ "$host" != "$HETZNER_HOST" ]]; then
        info "Log-Sicherung: nur auf dem Dev-Host aktiv, ${host} uebersprungen (${container})"
        return 0
    fi
    if [[ "$DRY_RUN" == "true" ]]; then
        info "[DRY-RUN] Log-Sicherung ${container} auf ${host} nach ${LOG_SICHERUNG_BASIS}/${container}/"
        return 0
    fi
    local out rc
    out=$(rssh_run "$host" <<EOF
c='${container}'
basis='${LOG_SICHERUNG_BASIS}'
scanner='${LOG_SICHERUNG_GITLEAKS}'
fund_rc=${LOG_SICHERUNG_FUND_RC}
warn_gb=${LOG_SICHERUNG_WARN_GB}
$(declare -f _log_sichern_remote)
_log_sichern_remote
EOF
    ) && rc=0 || rc=$?
    # Die Ausgabe traegt nur Pfade, Zahlen, Hashes und Regel-IDs.
    while IFS= read -r zeile; do
        [[ -n "$zeile" ]] || continue
        case "$zeile" in
            FUND*|WARN*) warn "Log-Sicherung ${container}: ${zeile}" ;;
            FEHLER*)     error_ "Log-Sicherung ${container}: ${zeile}" ;;
            *)           info "Log-Sicherung ${container}: ${zeile}" ;;
        esac
    done <<< "$out"
    if (( rc != 0 )) || ! grep -qE '^(GESICHERT|KEIN_CONTAINER) ' <<< "$out"; then
        error_ "Log-Sicherung ${container} auf ${host} NICHT belegt (rc=${rc})"
        return 1
    fi
    return 0
}

# Laeuft auf dem Host (per rssh_run, bash -s mit set -euo pipefail).
# Eingaben als Shell-Variablen: c, basis, scanner, fund_rc, warn_gb.
_log_sichern_remote() {
    local id
    id=$(docker ps -a --no-trunc --filter "name=^/${c}\$" --format '{{.ID}}')
    if [[ -z "$id" ]]; then
        echo "KEIN_CONTAINER ${c}"
        return 0
    fi
    # Scanner zuerst: ohne belegten Scanner wird nichts als gesichert gemeldet.
    # Nur eine Digest-Referenz zaehlt, und der lokale RepoDigest muss sie tragen.
    if [[ "$scanner" != *@sha256:* ]]; then
        echo "FEHLER scanner_ohne_digest ${scanner}"
        return 1
    fi
    local digests
    digests=$(docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$scanner" 2>/dev/null) || {
        echo "FEHLER scanner_fehlt ${scanner}"
        return 1
    }
    if ! grep -qxF "$scanner" <<< "$digests"; then
        echo "FEHLER scanner_digest_abweichend ${scanner}"
        return 1
    fi
    local logpfad meta
    logpfad=$(docker inspect -f '{{.LogPath}}' "$id")
    meta=$(docker inspect -f '{{.Image}} {{index .Config.Labels "bridge.git.commit"}} {{.HostConfig.LogConfig.Type}}' "$id")
    if [[ -z "$logpfad" || ! -f "$logpfad" ]]; then
        echo "FEHLER logdatei_fehlt ${logpfad:-<leer>}"
        return 1
    fi
    local stempel ordner
    stempel=$(date -u +%Y%m%dT%H%M%SZ)
    ordner="${basis}/${c}/${stempel}-${id:0:12}"
    umask 077
    mkdir -p "${basis}/${c}"
    chmod 0700 "$basis" "${basis}/${c}"
    mkdir "$ordner"
    mkdir "${ordner}/roh"
    local manifest="${ordner}/MANIFEST"
    {
        echo "container ${c}"
        echo "container_id ${id}"
        echo "image_commit_logtyp ${meta}"
        echo "logpfad ${logpfad}"
        echo "gesichert_am ${stempel}"
        echo "scanner ${scanner}"
    } > "$manifest"
    local quelle name groesse soll ist zeilen erste letzte n=0
    for quelle in "$logpfad" "$logpfad".*; do
        [[ -f "$quelle" ]] || continue
        name=$(basename "$quelle")
        cp --preserve=timestamps "$quelle" "${ordner}/roh/${name}"
        chmod 0600 "${ordner}/roh/${name}"
        groesse=$(stat -c %s "${ordner}/roh/${name}")
        soll=$(head -c "$groesse" "$quelle" | sha256sum | cut -d' ' -f1)
        ist=$(sha256sum < "${ordner}/roh/${name}" | cut -d' ' -f1)
        if [[ "$soll" != "$ist" ]]; then
            echo "FEHLER kopie_abweichend ${name}"
            echo "ergebnis FEHLER kopie_abweichend ${name}" >> "$manifest"
            return 1
        fi
        zeilen=$(wc -l < "${ordner}/roh/${name}")
        erste=$(head -n1 "${ordner}/roh/${name}" | grep -o '"time":"[^"]*"' | tail -n1 || true)
        letzte=$(tail -n1 "${ordner}/roh/${name}" | grep -o '"time":"[^"]*"' | tail -n1 || true)
        echo "datei ${name} bytes=${groesse} sha256=${ist} zeilen=${zeilen} erste=${erste:-?} letzte=${letzte:-?}" >> "$manifest"
        n=$((n + 1))
    done
    if (( n == 0 )); then
        echo "FEHLER keine_logdatei_kopiert"
        return 1
    fi
    # gitleaks ueber die unveraenderten Kopien, vor gzip. Der Scanner laeuft als
    # Eigentuemer des Ordners (Host: root) ohne Netz, ohne Capabilities, mit
    # EINEM Mount, nur lesend; der (redigierte) Bericht kommt ueber stdout.
    local besitzer grc=0
    besitzer=$(stat -c '%u:%g' "$ordner")
    docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
        --user "$besitzer" -v "${ordner}/roh:/scan:ro" \
        "$scanner" dir /scan --redact --no-banner --log-level error \
        --exit-code "$fund_rc" --report-format json --report-path - \
        > "${ordner}/GITLEAKS.json" 2>/dev/null || grc=$?
    if (( grc == fund_rc )); then
        local funde
        funde=$( (grep -o '"RuleID": *"[^"]*"' "${ordner}/GITLEAKS.json" || true) | wc -l)
        echo "gitleaks FUND ${funde}" >> "$manifest"
        echo "FUND gitleaks ${funde} Treffer, Bericht ${ordner}/GITLEAKS.json (redigiert):"
        # Nur Regel und Ort, nie der Wert.
        tr -d '\n' < "${ordner}/GITLEAKS.json" \
            | grep -o '"RuleID": *"[^"]*"\|"File": *"[^"]*"\|"StartLine": *[0-9]*' \
            | paste - - - | sed 's/"//g; s/^/FUND   /' | head -n 50 || true
    elif (( grc == 0 )); then
        echo "gitleaks 0" >> "$manifest"
    else
        echo "FEHLER scanner_lauf rc=${grc}"
        echo "ergebnis FEHLER scanner_lauf rc=${grc}" >> "$manifest"
        return 1
    fi
    chmod 0600 "${ordner}/GITLEAKS.json" 2>/dev/null || true
    # Packen, je Datei Rueckprobe gegen den MANIFEST-Hash.
    local datei hash
    for datei in "${ordner}"/roh/*; do
        name=$(basename "$datei")
        hash=$(sed -n "s/^datei ${name//./\\.} bytes=[0-9]* sha256=\([0-9a-f]*\) .*/\1/p" "$manifest")
        gzip -9 "$datei"
        ist=$(zcat "${datei}.gz" | sha256sum | cut -d' ' -f1)
        if [[ -z "$hash" || "$ist" != "$hash" ]]; then
            echo "FEHLER gzip_rueckprobe ${name}"
            echo "ergebnis FEHLER gzip_rueckprobe ${name}" >> "$manifest"
            return 1
        fi
    done
    echo "ergebnis GESICHERT dateien=${n}" >> "$manifest"
    chmod 0600 "$manifest"
    local gesamt_kb
    gesamt_kb=$(du -sk "$basis" | cut -f1)
    if (( gesamt_kb > warn_gb * 1024 * 1024 )); then
        echo "WARN ablage ${basis} hat $((gesamt_kb / 1024 / 1024)) GB (Grenze ${warn_gb} GB, kein automatisches Loeschen)"
    fi
    echo "GESICHERT ${ordner} dateien=${n} ablage_kb=${gesamt_kb}"
}
