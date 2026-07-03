# Manuelle Tasks: AWS Bedrock Setup (Per-User DSGVO-Routing)

## Status: ⏳ Ausstehend (Code-Seite FERTIG — es fehlt nur das AWS-Konto-Setup)

## Übersicht

Die Bridge kann User-Traffic per `users.provider_config` auf AWS Bedrock
(eu-central-1, EU-Datenresidenz) pinnen. Die komplette Code-Kette ist
implementiert und deployt sich mit dem normalen Bridge-Deploy:

- **Routing:** `src/routing/user_provider_override.py` erzwingt den Pin
  serverseitig (Chat + Research). Pin ohne AWS-Credentials → 503, KEIN
  stiller Fallback.
- **Billing:** Bedrock-Calls (streaming + non-streaming) buchen in
  `usage_events` mit `provider='bedrock'` + `aws_request_id` und ziehen
  User-Budget ab wie jeder andere Call.
- **Reconciliation:** Nightly-Job vergleicht unsere Tagessummen gegen
  CloudWatch `AWS/Bedrock` Token-Counts (beide Richtungen, Drift > 0.5% →
  ERROR-Log). Einsehbar: `GET /v1/metrics/bedrock-reconciliation`,
  manuell: `POST /v1/metrics/bedrock-reconciliation/run?day=YYYY-MM-DD`.
- **Panel:** Platform-Admin → Users-Tab → User anklicken → „Auf Bedrock
  pinnen (EU)" / „Pin entfernen".

Was NUR Rafael machen kann (AWS-Konsole), steht unten.

---

## 1. AWS Bedrock Model Access aktivieren

**Dauer:** ~10 Minuten · **Wer:** Rafael (AWS Account Owner)

1. **Konsole:** https://eu-central-1.console.aws.amazon.com/bedrock/home?region=eu-central-1#/modelaccess
   — Region **eu-central-1 (Frankfurt)**.
2. **Model Access beantragen** für die Anthropic-Modelle, die die Bridge
   tatsächlich routet (SSoT: `src/model_registry.py`; Bedrock-IDs =
   `eu.anthropic.<modell>-v1:0` via Cross-Region Inference Profile):
   - Claude Sonnet (aktuelle Generation) — Haupt-Arbeitsmodell
   - Claude Haiku (aktuelle Generation) — günstige Tasks
   - Claude Opus nur falls wirklich gebraucht (teuer)
3. First-Time-Usage-Formular ausfüllen (Use Case, Firma) — Approval meist
   in Minuten.
4. **Wichtig:** EU Cross-Region Inference Profile (`eu.` Prefix) routet
   innerhalb der EU (Frankfurt/Irland/Paris/Stockholm) — DSGVO-seitig ok,
   aber Model Access muss in den Profil-Regionen gewährt sein (die Konsole
   zeigt das beim Profil an).

## 2. IAM-User (minimal privilege)

Eigener IAM-User nur für die Bridge, KEINE Root-Keys. Policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        "arn:aws:bedrock:*:*:inference-profile/eu.anthropic.*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics"],
      "Resource": "*"
    }
  ]
}
```

(Der CloudWatch-Teil ist die Grundlage der 1:1-Abrechnungs-Reconciliation.)

## 3. Model Invocation Logging aktivieren (empfohlen)

Bedrock-Konsole → Settings → Model invocation logging → CloudWatch Logs.
Loggt pro Invocation u.a. Token-Counts + RequestId. Unsere `usage_events`
tragen `provider_metadata.aws_request_id` → Call-Level-Join für forensische
Prüfung einzelner Abrechnungen (die Tages-Reconciliation läuft auch ohne,
über CloudWatch-Metriken).

## 4. Credentials hinterlegen

**Keys gehören in `secrets/platform.env` auf den Bridge-Servern** — die
Datei ist bereits als `env_file` an alle Worker gebunden (dev-Overlay +
`docker-compose-prod-platform.yml`). KEINE Compose-Änderung nötig:

```bash
# auf dem Bridge-Server, ans Ende von secrets/platform.env:
AWS_ACCESS_KEY_ID_BEDROCK=AKIA...
AWS_SECRET_ACCESS_KEY_BEDROCK=...
AWS_REGION_BEDROCK=eu-central-1
```

Danach normaler Deploy/Restart (`bridge-deploy.sh`). Zusätzlich die drei
Werte in Infisical (`dev-server`-Projekt) ablegen, damit sie eine SSoT
außerhalb des Servers haben.

## 5. Verifizieren

```bash
# 1. Credentials erkannt? (Worker-Log beim Start)
docker logs wt-wrapper-worker1 2>&1 | grep -i "Bedrock credentials"
#    → "✅ Bedrock credentials configured (region=eu-central-1)"

# 2. Expliziter Bedrock-Call (Request-Feld backend, ohne User-Pin):
curl -sS -X POST $AI_BRIDGE_URL/v1/chat/completions \
  -H "Authorization: Bearer $AI_BRIDGE_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"sonnet","backend":"bedrock","messages":[{"role":"user","content":"Sag nur OK"}]}'
#    → x_backend_info.backend == "bedrock", region eu-central-1, aws_request_id gesetzt

# 3. User pinnen (Panel oder API) und App-Call des Users prüfen:
#    Platform-Admin → Users → User → "Auf Bedrock pinnen (EU)"
#    usage_events: SELECT provider, region, input_tokens, output_tokens,
#                  provider_metadata->>'aws_request_id'
#                  FROM usage_events WHERE provider='bedrock'
#                  ORDER BY recorded_at DESC LIMIT 5;

# 4. Reconciliation manuell anstoßen (Admin-Token):
curl -sS -X POST "$AI_BRIDGE_URL/v1/metrics/bedrock-reconciliation/run" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

## Checkliste

- [ ] Model Access granted (eu-central-1, EU-Inference-Profile)
- [ ] IAM-User + Minimal-Policy (invoke + cloudwatch read)
- [ ] Model Invocation Logging an
- [ ] Keys in `secrets/platform.env` (beide Bridge-Server) + Infisical
- [ ] Worker-Log: „Bedrock credentials configured"
- [ ] Test-Call mit `backend:"bedrock"` → `x_backend_info.backend=bedrock`
- [ ] Test-User gepinnt → `usage_events.provider='bedrock'` Rows
- [ ] Reconciliation-Run → status `ok`

## Kosten & Grenzen (bewusste Entscheidungen)

- Bedrock = Anthropic-**Listenpreise**, abgerechnet über AWS. Der direkte
  Bedrock-Pfad nutzt **kein Prompt-Caching und kein Tool-Use** → real
  teurer pro Call als die Abo-Accounts. Bedrock-Pins daher nur für
  Production-User mit echter pay-per-token-Marge.
- Monats-Abgleich der AWS-Rechnung (Cost Explorer) gegen
  `SUM(usage_events.real_cost_eur) WHERE provider='bedrock'` bleibt ein
  manueller Schritt (bewusst: Cost-Explorer-API kostet pro Call und die
  Rechnung kommt ohnehin monatlich).

## Quellen

- Model Access: https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html
- Inference Profiles: https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html
- CloudWatch-Metriken: https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-cw.html
- Pricing: https://aws.amazon.com/bedrock/pricing/
