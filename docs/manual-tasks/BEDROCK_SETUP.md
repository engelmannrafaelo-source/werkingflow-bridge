# Manuelle Tasks: AWS Bedrock Setup (DSGVO-Modus)

## Status: ⏳ Ausstehend

## Übersicht

Für den DSGVO-konformen Bedrock-Modus muss einmalig der Model Access in AWS aktiviert werden.

---

## 1. AWS Bedrock Model Access aktivieren

**Dauer:** ~5 Minuten
**Wer:** Rafael (AWS Account Owner)

### Schritte:

1. **AWS Console öffnen:**
   - URL: https://eu-central-1.console.aws.amazon.com/bedrock/home?region=eu-central-1#/modelaccess
   - Region: **eu-central-1 (Frankfurt)** - WICHTIG für DSGVO!

2. **Model Access beantragen:**
   - Klick auf "Manage model access"
   - Folgende Modelle aktivieren:
     - ☐ `anthropic.claude-3-5-sonnet-20241022-v2:0` (Claude 3.5 Sonnet)
     - ☐ `anthropic.claude-3-5-haiku-20241022-v1:0` (Claude 3.5 Haiku)
     - ☐ `anthropic.claude-3-opus-20240229-v1:0` (Claude 3 Opus) - optional
   - "Request model access" klicken

3. **First-Time Usage Form ausfüllen:**
   - Anthropic verlangt einmalig ein Formular
   - Typische Fragen: Use Case, Company, Expected Usage
   - Approval: Normalerweise innerhalb von Minuten

4. **Access bestätigen:**
   - Status sollte auf "Access granted" wechseln
   - Bei Problemen: AWS Support kontaktieren

---

## 2. IAM Permissions prüfen

**User:** `AKIAYQYUBB2GC5GTWATM`

### Benötigte Permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "arn:aws:bedrock:eu-central-1::foundation-model/anthropic.*"
    }
  ]
}
```

### Prüfen:
1. IAM Console: https://console.aws.amazon.com/iam/
2. User `AKIAYQYUBB2GC5GTWATM` finden
3. Permissions prüfen oder Policy hinzufügen

---

## 3. Bedrock-Container starten (nach Model Access)

```bash
# Auf Hetzner:
ssh root@49.12.72.66

# Dateien synchronisieren
cd /root/werkingflow-bridge

# Bedrock-Modus starten
cd docker
docker compose -f docker-compose.yml -f docker-compose.bedrock.yml --env-file .env.bedrock up -d

# Health Check
curl http://localhost:8000/health
```

---

## 4. Testen

```bash
# Einfacher Test
curl -X POST http://49.12.72.66:8000/v1/chat/completions \
  -H "Authorization: Bearer test" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-3-5-sonnet-20241022", "messages": [{"role": "user", "content": "Hello"}]}'
```

---

## Checkliste

- [ ] AWS Bedrock Model Access beantragt (eu-central-1)
- [ ] First-Time Usage Form ausgefüllt
- [ ] Model Access granted
- [ ] IAM Permissions geprüft
- [ ] Bedrock-Container gestartet
- [ ] Health Check erfolgreich
- [ ] Test-Request funktioniert

---

## Kosten

| Modell | Input (1M tokens) | Output (1M tokens) |
|--------|-------------------|---------------------|
| Claude 3.5 Sonnet | $3.00 | $15.00 |
| Claude 3.5 Haiku | $0.80 | $4.00 |
| Claude 3 Opus | $15.00 | $75.00 |

**Empfehlung:** Haiku für Research, Sonnet für komplexe Tasks.

---

## Quellen

- [AWS Bedrock Model Access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)
- [Claude on Bedrock Regions](https://docs.aws.amazon.com/bedrock/latest/userguide/models-regions.html)
- [Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
