# Energy: Abo-Rückfall nur auf der Dev-Bridge

Grundlage: Rafaels Entscheid vom 01.11.2026 (Energy auf Staging mit vorhandenen
Abo-Kontingenten und Promotional-Guthaben weiterentwickeln, echte Zusatzkosten
weiterhin verhindern). Code: `src/providers/funding_policy.py`.

## Vertrag

- `X-App-ID: werking-energy` bleibt `promotional_only`: der Guthaben-Pool ist
  erste Wahl.
- Nur wenn **beide** Werte in der Worker-Umgebung stehen, darf Energy auf das
  Abo laufen: `ENERGY_STAGING_SUBSCRIPTION_FALLBACK` wahr und
  `BRIDGE_ORIGIN_ID` in `SUBSCRIPTION_FALLBACK_BRIDGES` (heute nur `dev`, die
  vorhandene Bridge-Kennung aus ADR-0011/0012). Kein Request-Header, keine
  Quell-IP, kein Mandant entscheidet. Ohne Kennung (Prod) bleibt alles wie
  bisher: 503 ohne Koordinator, 429 ohne Budget.
- Auslöser: Pool nicht konfiguriert (`credit_pool_missing`) oder
  `credit_pool.PoolUnavailable` (Koordinator fehlt/unerreichbar/lehnt ab, kein
  Konto mit Budget; immer bevor eine Messages-Anfrage gesendet wurde) →
  `credit_pool_unavailable`. Andere Fehler (400, Anthropic-Fehler, verlorenes
  angeheftetes Konto) werden nicht umgeleitet.
- Abo-Weg = normaler CLI-Weg: dieselbe Authentifizierung, Budgetsperre,
  `enforce_pool_admission` bzw. `_admit_research_to_pool` (Limiter, keine
  Umgehung). Nur Backend `anthropic` (CLI). Alles andere — direkter API-Schlüssel,
  Bedrock, Gemini, OpenAI-kompatibel, Bildanfragen, Werkzeuge, Sitzungen,
  Text-Wegwahl auf den direkten Weg, Perplexity — wird mit 409 bzw. Fehler
  abgewiesen oder abgeschaltet, nie umgeleitet. `automatic_paid_fallback` bleibt
  `false`.
- Nicht umgestellt (bleiben strikt): Dokumentkonvertierung mit
  Bildbeschreibungen (kein Abo-Bildweg), `/v1/doc-agent`.

## Sichtbarkeit

- Chat: `x_funding = {funding_policy, automatic_paid_fallback, funding_fallback_cause?}`
  im JSON, sonst Header `X-Bridge-Funding-Policy`.
- Recherche: `ResearchResponse.funding_policy`.
- Ledger: `provider_metadata.funding_policy`, `funding_fallback_cause`,
  `automatic_paid_fallback` (aus einer Kontextvariable der Anfrage; explizite
  Aufruf-Metadaten gewinnen). Werte: `promotional_only` |
  `subscription_staging_fallback`. Ein späterer Zweig „Finanzierungsquelle“ darf
  diese Felder erweitern, nicht umdeuten.
- `real_cost_eur`: Abo-Zeilen tragen Provider `anthropic`; `resolve_ledger_cost`
  bucht dafür bei Abo-Mandanten 0 (hypothetical bleibt). Bei
  `pay_per_token`-Mandanten gilt unverändert real = hypothetical.

## Pyramiden-Szenarien

Pool leer bei Chat und Recherche (synchron, asynchron, Job-Selbstaufruf);
Koordinator nicht erreichbar; Pool während einer Recherche erschöpft (erste
Runde vs. nach Anheftung); Abo-Pool gleichzeitig voll (Limiter antwortet,
kein bezahlter Weg); Railway-Staging-Backend als Aufrufer (nur `X-App-ID`,
fremde Quell-IP); dieselbe Anfrage auf der Prod-Bridge (strikt);
Kennung nur halb gesetzt; Bild-, Werkzeug-, Sitzungs- und Fremd-Tier-Anfragen
im Rückfall; Ledger-Zeile und Antwort tragen die Kennzeichnung.

Zusätzlicher Pyramiden-Prüfkontext: Default AUS; Header können die Kennung
nicht ersetzen; Prod mit versehentlich gesetztem Schalter bleibt strikt;
Rückfall ändert keine Pool-Reservierung; Rücknahme = Schalter entfernen und
Worker neu ausrollen.

Unit-Tests ohne Netz: `tests/unit/test_energy_staging_subscription_fallback.py`.
