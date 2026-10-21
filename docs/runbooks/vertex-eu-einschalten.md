# Vertex EU (backend `vertex_eu`) einschalten

Claude über Google Vertex AI, EU-Multiregion (`locations/eu`), Code in
`src/vertex_eu_service.py`. Vorgebaut 21.10.2026, **standardmäßig aus**.

## Was der Weg garantiert

- Erreichbar nur über den Operator-Pin `users.provider_config = {"provider": "vertex_eu"}`.
  Ein Client-`backend=vertex_eu` ohne Pin ist 403 (`vertex_eu_requires_operator_pin`),
  auch bei Apps mit App-Regel (wird nicht still auf den Pool umgeschrieben).
- Pin ohne Schalter oder ohne Zugangsdaten: 503 `user_provider_override_unavailable`,
  kein Rückfall auf ein anderes Backend.
- Umgebung: prod; staging/local nur mit `VERTEX_EU_ALLOW_NON_PROD=1`. Fehlendes
  `X-App-Env` nie. Fehlendes `X-App-ID` → 400. Anders als beim Bedrock-Pin wird
  außerhalb von prod **nicht** still auf Anthropic heruntergestuft.
- `enable_tools=true` mit Vertex-Pin → 503 (keine Claude-Code-Tools auf diesem Weg).
- Research mit Vertex-Pin → Fehler (keine Websuche, keine Ausnahme wie bei Bedrock).
- Buchung in `usage_events` mit `provider='vertex-eu'` (Echtgeld, `REAL_COST_PROVIDERS`),
  `provider_metadata` mit `vertex_model_id`, `location`, `provider_request_id` (`msg_vrtx_…`).
- Quote: 429 mit `quota_limit_value: "0"` → `vertex_eu_quota_zero`, `retryable:false`;
  sonstige 429 → `vertex_eu_quota_exceeded`, `retryable:true`. Der Quotenname steht in der Meldung.
- 401/403 → 424 `vertex_eu_permission_denied`, 404 → 424 `vertex_eu_model_not_found`.

## Reihenfolge beim Einschalten

1. **Migration `docker/migrations/069_usage_events_provider_vertex_eu.sql`** auf der Ziel-DB
   einspielen. Vorher scheitern die Ledger-Inserts am CHECK-Constraint.
2. `GCP_VERTEX_SA_JSON` (JSON-Text oder Pfad) aus Infisical in die Bridge-Umgebung.
   `project_id` kommt aus dem Schlüssel. Nie loggen, nie committen.
3. `VERTEX_EU_ENABLED=1` (und für Tests auf dev: `VERTEX_EU_ALLOW_NON_PROD=1`).
4. Quote `eu_multi_region_online_prediction_requests_per_base_model` je Modell > 0
   (am 21.10.2026 noch 0 → jeder Aufruf 429).
5. Einen **Testnutzer** pinnen, nie einen echten Kunden ohne Rafaels Freigabe.

Modelle: nur die in `VERTEX_MODEL_IDS` eingetragenen (`claude-sonnet-5-5`,
`claude-haiku-5-5`); alles andere ist ein lauter 400.

Optional: `VERTEX_EU_LOCATION` (Vorgabe `eu`), `VERTEX_EU_ENDPOINT`
(Vorgabe `https://aiplatform.eu.rep.googleapis.com`).
