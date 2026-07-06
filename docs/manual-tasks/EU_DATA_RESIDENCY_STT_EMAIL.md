# EU Data Residency — Speech-to-Text (Whisper) & Transactional Email (Resend)

**Ziel:** Die letzten zwei US-Sub-Auftragsverarbeiter für die WerkING Tools (Report/Energy/
Engelmann/Noise) auf EU-Residenz umstellen. Alles Übrige ist bereits EU (Hetzner, AWS Bedrock
Frankfurt, Vercel fra1, Mathpix eu-central-1, ConvertAPI Litauen, Mollie NL).
**Sentry NICHT anfassen** — wird separat entfernt (Rafael 2026-07-06).

Recherche-Stand: 2026-07-06 (AI-Bridge `/v1/research`, offizielle AWS-Docs + Resend Regions).

---

## 1. Speech-to-Text / Whisper (AI-Bridge) — CODE fertig, ACCOUNT offen

### Entscheidung: AWS statt Azure (Rafael 2026-07-06)

AWS ist bereits als Sub-Prozessor angebunden (Claude läuft über **AWS Bedrock, eu-central-1**) —
also **kein neues Azure-Konto**. **AWS Bedrock hostet aber KEIN Whisper-Modell**, daher bleiben
zwei AWS-Wege: **Amazon Transcribe** oder **Whisper selbst-gehostet auf SageMaker**.

**Gewählt: Whisper large-v3 auf Amazon SageMaker (Real-Time-Endpoint, eu-central-1).** Begründung:

| Kriterium | SageMaker-Whisper (gewählt) | Amazon Transcribe (verworfen) |
|-----------|-----------------------------|-------------------------------|
| **Synchron?** (der Bridge-Endpoint ist ein synchroner Datei→Text-Call) | ✅ Real-Time-Endpoint = 1 synchroner `invoke_endpoint`-Call | ❌ **Kein synchroner Modus** — nur Batch (S3, async) oder Streaming (HTTP/2-Eventstream) |
| **Qualität DE-Fachdiktat** | ✅ Whisper large-v3, ~2,6 % WER Deutsch, robust bei Akzent/Rauschen | ⚠️ generisch, für DE-Fachvokabular schwächer |
| **Regression** | ✅ **null** — identisches Whisper-Modell wie heute | ⚠️ ASR-Qualität ändert sich |
| **Bridge-Integration** | ✅ boto3 `sagemaker-runtime`, SigV4 automatisch — **exakt wie der Bedrock-Pfad**, reuse der Bedrock-AWS-Creds | ❌ Transcoding (PCM/16 kHz) + Eventstream-Client nötig |
| **Ops/Kosten** | ⚠️ Real-Time-GPU-Endpoint = **stehende Kosten** (kein scale-to-zero) | ✅ managed, pay-per-use |

Der einzige echte Nachteil von SageMaker ist die **stehende GPU-Instanz** (kein scale-to-zero;
**SageMaker Serverless hat KEINE GPU → für Whisper nicht nutzbar**; Async-Inference skaliert auf
null, ist aber **nicht synchron** und würde den Endpoint-Kontrakt brechen). Das ist eine
**Kosten-Entscheidung für Rafael**, keine Code-Frage.

### Was im Code passiert ist (dieser Commit, `werkingflow-bridge` develop)

Der STT-Endpoint `POST /v1/audio/transcriptions` (`src/main.py`) ist **provider-pluggable**
(`_resolve_stt_provider()` + Dispatch), env-gesteuert, **fail-loud, kein Silent-US-Fallback**:

| `STT_PROVIDER` | Verhalten |
|----------------|-----------|
| `openai` *(Default, unverändert)* | OpenAI-US-Proxy. **Nichts ändert sich, bis EU-Vars gesetzt sind.** |
| `openai` + `OPENAI_STT_BASE_URL=https://eu.api.openai.com/v1` | Option B (zero-Infra-Fallback): OpenAIs eigene EU-Residenz (nur Base-URL + EU-Key). |
| `aws-sagemaker` | **Gewählter EU-Weg:** synchroner `invoke_endpoint` gegen den SageMaker-Whisper-Endpoint, reuse der Bedrock-AWS-Creds (`bedrock_credential_manager`). |

Neue Env-Vars (in `docker-compose*.yml` an alle Worker durchgereicht; AWS-Creds kommen bereits
via `env_file` wie bei Bedrock):

| Var | Default | Zweck |
|-----|---------|-------|
| `STT_PROVIDER` | `openai` | `openai` (US) oder `aws-sagemaker` (EU) |
| `AWS_STT_SAGEMAKER_ENDPOINT` | *(leer)* | Name des SageMaker-Endpoints, z.B. `whisper-large-v3-eu` |
| `AWS_STT_REGION` | *(leer→`eu-central-1`)* | Region; default = Bedrock-Region (Frankfurt) |
| `OPENAI_STT_BASE_URL` | *(leer)* | nur für Option B (OpenAI-EU) |

**Verifikationsstand:** Resolver-Dispatch + Response-Normalisierung + Fail-loud-Pfade sind gegen
den echten Quellcode unit-getestet (13 Fälle grün). Der **Live-`invoke_endpoint`-Call ist noch
NICHT gegen einen echten Endpoint verifiziert** (es existiert noch keiner — Rafaels Account-
Aktion). Der Pfad ist **off-by-default** und fail-loud: bei Kontrakt-Abweichung wirft er, statt
still Falsches zu liefern. Der OpenAI-Default-Pfad ist unangetastet → **nichts bricht**.

### 🔴 Was RAFAEL im Account tun muss

1. **SageMaker-Endpoint deployen** — Whisper large-v3 in **eu-central-1** über die **HuggingFace
   ASR Inference Toolkit (HF DLC)**. Wichtig: **HF-ASR-Toolkit, NICHT das JumpStart-Paketmodell** —
   der Bridge-Code erwartet den HF-Kontrakt: **rohe Audio-Bytes rein (`audio/*` content-type, HF
   dekodiert mp3/webm/wav via ffmpeg intern + resampled auf 16 kHz), JSON `{"text": "..."}` raus.**
   (Das JumpStart-large-v3-Paket verlangt zusätzlich einen expliziten `language`-Parameter und hat
   ein 30-Sekunden-Limit — anderer Payload-Kontrakt; wenn du das statt HF-DLC nimmst, muss der
   `invoke_endpoint`-Payload in `_sagemaker_transcribe_sync` angepasst werden.)
   - Instanz-Typ: GPU, z.B. `ml.g4dn.xlarge` (Real-Time → stehende Kosten).
   - Grenzen Real-Time-Endpoint: **Payload < 6 MB**. Für Langform-Diktat die HF-Pipeline mit
     `chunk_length_s` konfigurieren (die Diktat-Clips der Apps sind i.d.R. kurz).
2. **IAM:** dem bereits genutzten Bedrock-AWS-Key die Permission **`sagemaker:InvokeEndpoint`** für
   diesen Endpoint geben (gleicher Key, nur zusätzliche Policy — keine neuen Credentials nötig).
3. **Env setzen** (Infisical `dev-server` bzw. Bridge-Host-`.env`, das via `env_file` in die
   Container fließt):
   ```
   STT_PROVIDER=aws-sagemaker
   AWS_STT_SAGEMAKER_ENDPOINT=<endpoint-name>
   # AWS_STT_REGION optional, default eu-central-1
   ```
4. **Bridge deployen:** `scripts/bridge-deploy.sh both` (gated, Rollback) — **nur Rafael**, Prod
   bedient zahlende Kunden (siehe Bridge-CLAUDE.md).
5. **Live verifizieren** (der von mir nicht abdeckbare Schritt): eine echte Diktat-Aufnahme in einer
   App durchführen ODER direkt `POST /v1/audio/transcriptions` mit Testaudio gegen die Bridge; auf
   `{"text": ...}` prüfen und in SageMaker-Metriken den Call in eu-central-1 sehen. Wirft der
   Endpoint statt `{"text":...}` etwas anderes → Deployment-Kontrakt (Punkt 1) angleichen.

**Alternative ohne SageMaker-Ops (Option B):** OpenAI eigene EU Data Residency — nur
`OPENAI_STT_BASE_URL=https://eu.api.openai.com/v1` + ein in EU-Region angelegtes OpenAI-Projekt/
Key. Kein AWS-Endpoint, kein Ops, aber weiterhin OpenAI als Sub-Prozessor (nur EU-resident).

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
- (a) Resend EU-Dispatch akzeptieren (Transit EU, Metadaten US) — pragmatisch, schnell; **oder**
- (b) auf einen echten EU-Provider migrieren — naheliegend **AWS SES `eu-central-1`/Frankfurt**
  (passt zur bestehenden AWS-EU-Nutzung, ein Sub-Prozessor weniger), oder Postmark/SendGrid EU.
  Das **wäre** ein Code-Change in den `email.ts` und eine separate Aufgabe.

Dies ist eine **Produkt-/Compliance-Intent-Frage** → nicht autonom entscheidbar.

---

## Status-Zusammenfassung

| Punkt | Code | Account (Rafael) |
|-------|------|------------------|
| STT EU-fähig (provider-pluggable, fail-loud, aws-sagemaker + OpenAI-EU) | ✅ fertig, unit-getestet, off-by-default | 🔴 SageMaker-Whisper-Endpoint (HF-DLC) eu-central-1 + IAM `sagemaker:InvokeEndpoint` + Env + Bridge-Deploy + **Live-Verify** |
| E-Mail EU-Versand | ✅ nichts zu tun (Domain-Setting) | 🔴 Resend-Domain-Region → eu-west-1 |
| E-Mail volle EU-Datenresidenz | — | 🔴 Entscheidung Resend-EU vs. AWS SES eu-central-1 |
| Sentry | — | (separat, NICHT hier) |
