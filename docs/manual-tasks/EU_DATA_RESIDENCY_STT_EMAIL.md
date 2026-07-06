# EU Data Residency — Speech-to-Text (Whisper) & Transactional Email (Resend)

**Ziel:** Die letzten zwei US-Sub-Auftragsverarbeiter für die WerkING Tools (Report/Energy/
Engelmann/Noise) auf EU-Residenz umstellen. Alles Übrige ist bereits EU (Hetzner, AWS Bedrock
Frankfurt, Vercel fra1, Mathpix eu-central-1, ConvertAPI Litauen, Sentry EU, Mollie NL).

Recherche-Stand: 2026-07-06 (AI-Bridge `/v1/research`, offizielle Docs: OpenAI EU Data Residency,
Azure OpenAI Audio REST, Resend Regions).

---

## 1. Speech-to-Text / Whisper (AI-Bridge) — CODE fertig, ACCOUNT offen

### Was im Code passiert ist (dieser Commit)

Der STT-Proxy `POST /v1/audio/transcriptions` (`src/main.py`) ruft nicht mehr **hardcoded**
`https://api.openai.com` auf, sondern einen **env-gesteuerten Provider** (`_resolve_stt_upstream`).
Das App-Contract ist unverändert — alle Apps posten weiterhin dasselbe Multipart-Formular an die
Bridge. Nur die Bridge → Upstream-Kante wird EU-fähig.

**Fail-loud, kein Silent-Fallback:** Ist ein EU-Provider gewählt aber unvollständig konfiguriert,
antwortet die Bridge mit `500 config_error` — sie fällt **niemals** still auf den US-Endpoint
zurück (das würde den GDPR-Zweck aushebeln).

Env-Vars (in `docker-compose*.yml` bereits an alle Worker durchgereicht, Default = bisheriges
Verhalten):

| Var | Default | Zweck |
|-----|---------|-------|
| `STT_PROVIDER` | `openai` | `openai` (US, unverändert) oder `azure` (EU) |
| `OPENAI_STT_BASE_URL` | *(leer)* | Für **OpenAI EU**: `https://eu.api.openai.com/v1` |
| `AZURE_OPENAI_STT_ENDPOINT` | *(leer)* | z.B. `https://<res>.openai.azure.com` |
| `AZURE_OPENAI_STT_KEY` | *(leer)* | Azure-Resource-Key |
| `AZURE_OPENAI_STT_DEPLOYMENT` | *(leer)* | Deployment-Name, z.B. `whisper` |
| `AZURE_OPENAI_STT_API_VERSION` | `2024-06-01` | Azure API-Version |

**Solange `STT_PROVIDER` ungesetzt/`openai` und kein `OPENAI_STT_BASE_URL` gesetzt ist, ändert
sich NICHTS** — STT läuft weiter über OpenAI-US (nicht gebrochen, aber noch nicht EU).

### 🔴 Was RAFAEL im Account tun muss (nicht im Code lösbar)

**Empfohlen: Azure OpenAI Whisper in EU-Region** (sofort verfügbar, kein Sales-Gate, identisches
API-Surface; Sweden Central hat Audio-Modelle bestätigt).

1. Azure OpenAI Resource in **Sweden Central** (oder France Central / West Europe) anlegen.
2. Ein **Whisper-Deployment** in dieser Resource erstellen (Deployment-Name notieren).
3. Endpoint-URL + Key aus dem Azure-Portal holen.
4. Werte in Infisical `dev-server` (oder wo die Bridge ihr Host-`.env` speist) setzen:
   ```
   STT_PROVIDER=azure
   AZURE_OPENAI_STT_ENDPOINT=https://<resource>.openai.azure.com
   AZURE_OPENAI_STT_KEY=<key>
   AZURE_OPENAI_STT_DEPLOYMENT=<whisper-deployment-name>
   # optional, sonst 2024-06-01:
   AZURE_OPENAI_STT_API_VERSION=2024-06-01
   ```
5. Bridge deployen: `scripts/bridge-deploy.sh both` (gated, Rollback) — **nur durch Rafael**,
   Prod bedient zahlende Kunden (siehe Bridge-CLAUDE.md).
6. Verifizieren: eine echte Diktat-Aufnahme in einer App durchführen ODER direkt
   `POST /v1/audio/transcriptions` mit Testaudio gegen die Bridge; Azure-Portal-Metriken sollten
   den Call in der EU-Region zeigen.

**Alternative: OpenAI eigene EU Data Residency** (einfacher im Code — nur Base-URL — aber
Sales-gated, neues EU-Projekt + EU-Region-Key nötig, 2026 Preis-Aufschlag):
```
OPENAI_STT_BASE_URL=https://eu.api.openai.com/v1
OPENAI_API_KEY=<key eines in EU-Region angelegten OpenAI-Projekts>
```
(`STT_PROVIDER` bleibt `openai`.)

---

## 2. Transaktions-E-Mail / Resend — KEIN Code-Change, nur ACCOUNT

Die Apps senden an `https://api.resend.com/emails` (`apps/*/src/lib/email.ts`). Resend bietet
**keine** regionale API-URL — die EU-Zustellung wird **pro Domain im Resend-Dashboard**
konfiguriert. Der bestehende Code sendet automatisch EU-resident, sobald die Absender-Domain auf
EU-Region steht. **Deshalb wurde am App-Code bewusst nichts geändert** (ein Code-Change wäre
Pfusch — es gibt nichts zu ändern).

### 🔴 Was RAFAEL im Account tun muss

1. Resend-Dashboard → **Domains** → für jede Absender-Domain (Report/Energy/Engelmann) die
   **Region auf `eu-west-1` (Irland)** stellen (bzw. Domain in EU-Region neu anlegen).
2. Danach: eine Test-E-Mail über jede App auslösen und im Resend-Log die Region prüfen.

### ⚠️ GDPR-Vorbehalt (Rafael-Entscheidung nötig)

Laut Resend-Doku steuert die Region **nur den Versand-Ort (Dispatch)**, **nicht** die
Datenspeicherung: E-Mail-Metadaten, Logs, API-Records und Analytics bleiben laut Recherche in den
**USA**, unabhängig von der Versand-Region. Für eine **strikte** Sub-Auftragsverarbeiter-EU-
Residenz reicht Resend-EU-Dispatch also möglicherweise **nicht**.

**Rafael muss entscheiden:**
- (a) Resend EU-Dispatch akzeptieren (Transit EU, Metadaten US) — pragmatisch, schnell, aber
  nicht vollständig EU-resident; **oder**
- (b) auf einen echten EU-Provider migrieren (z.B. AWS SES `eu-central-1`/Frankfurt — passt zur
  bestehenden AWS-EU-Nutzung, oder Postmark/SendGrid EU). Das **wäre** ein Code-Change in den
  `email.ts` und eine separate Aufgabe.

Dies ist eine **Produkt-/Compliance-Intent-Frage** → nicht autonom entscheidbar.

---

## Status-Zusammenfassung

| Punkt | Code | Account (Rafael) |
|-------|------|------------------|
| STT EU-fähig machen (Provider-Switch, fail-loud) | ✅ fertig (dieser Commit) | 🔴 Azure-EU-Resource + Env + Bridge-Deploy |
| E-Mail EU-Versand | ✅ nichts zu tun (Domain-Setting) | 🔴 Resend-Domain-Region → eu-west-1 |
| E-Mail volle EU-Datenresidenz | — | 🔴 Entscheidung Resend-EU vs. AWS SES eu-central-1 |
