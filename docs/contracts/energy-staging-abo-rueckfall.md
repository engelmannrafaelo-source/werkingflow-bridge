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
- Zusätzlich muss die **Herkunft der Anfrage** (ADR-0011, von nginx als
  `X-Bridge-Origin` = `$bridge_origin_out` gestempelt, im Worker
  `federation.get_request_origin()`) die eigene Bridge sein. Die Prod-Upstreams
  haben die Dev-Bridge als Backup (`docker/upstreams-prod.conf`, nginx wiederholt
  503/429 dort): eine Prod-Energy-Anfrage auf einem Dev-Worker trägt Herkunft
  `prod`, wird auf Prod abgerechnet und bleibt deshalb strikt. nginx setzt die
  Herkunft jedes nicht vertrauten Absenders (Railway-Staging, extern) auf die
  eigene `${BRIDGE_ID}`, solche Aufrufe kommen also als `dev` an. Ohne Herkunft
  (Direktaufruf am Worker, nie über den LB) gilt die Anfrage als eigene, dieselbe
  Regel wie `federation.is_foreign_origin()`.
- Auslöser: Pool nicht konfiguriert (`credit_pool_missing`) oder
  `credit_pool.PoolUnavailable` (Koordinator fehlt/unerreichbar/lehnt ab, kein
  Konto mit Budget; immer bevor eine Messages-Anfrage gesendet wurde) →
  `credit_pool_unavailable`. Andere Fehler (400, Anthropic-Fehler, verlorenes
  angeheftetes Konto) werden nicht umgeleitet.
- Recherche: Ist das Pool-Konto nach einer erfolgreichen Runde angeheftet, gilt
  ein späteres `PoolUnavailable` (Koordinator fällt aus; `GET /accounts` läuft vor
  jeder Runde) als verlorenes angeheftetes Konto: laut, kein Neustart auf dem Abo
  (kein Doppellauf), in der Rückrunde bleibt der Bericht von vorher.
- Im Abo-Lauf versucht `_run_api_reserve_fallback` den Pool nicht ein zweites Mal
  (429 `subscription_staging_fallback`), damit keine Pool-bezahlte Antwort als
  Abo-Antwort gekennzeichnet wird.
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

- Streaming: Der Pool-Weg ist Anfrage/Antwort; `PoolUnavailable` fällt immer
  vor dem ersten Stream-Byte, der Abo-Lauf startet dann den normalen CLI-Stream.
  Bei Streams steht die Kennzeichnung nur im Header `X-Bridge-Funding-Policy`
  (kein `x_funding` im Körper). Nicht geändert.

## Mandanten und Ledger

- Der Rückfall hängt nur an `X-App-ID`, Kennung und Herkunft, nie am Mandanten.
  Der Abo-Lauf durchläuft dieselbe Budgetsperre mit derselben Attribution wie der
  Pool-Lauf; deren Weigerungen (z. B. 409 `billing_plan_unresolved`) werden nicht
  umgeleitet oder umgedeutet (Test mit Mandant `phasentest`).
- Mandant ohne `tenants`-Eintrag (heute `phasentest`): `load_billing_context`
  liefert `None`, der Ledger-Schreiber überspringt die `usage_events`-Zeile mit
  Warn-Log (`skipped:no_tenant`) und bricht den Aufruf nicht — unverändert. Die
  Buchung trotzdem gehört in den Zweig „Finanzierungsquelle“.
- Energy-Staging sendet kein `X-Priority: production`; kein Handlungsbedarf.

## Offene Punkte

- Keine eigene Gleichzeitigkeitsgrenze für Rückfälle: Abo-Rückfälle teilen den
  normalen Pool-Limiter (`enforce_pool_admission`/`_admit_research_to_pool`) mit
  der übrigen Abo-Last. Eine vorhandene, workerübergreifende Struktur für eine
  eigene Grenze gibt es nicht; eine prozesslokale Semaphore wäre je Worker und
  keine echte Grenze.

## Pyramiden-Szenarien

Pool leer bei Chat und Recherche (synchron, asynchron, Job-Selbstaufruf);
Koordinator nicht erreichbar; Pool während einer Recherche erschöpft (erste
Runde vs. nach Anheftung); Abo-Pool gleichzeitig voll (Limiter antwortet,
kein bezahlter Weg); Railway-Staging-Backend als Aufrufer (nur `X-App-ID`,
fremde Quell-IP); dieselbe Anfrage auf der Prod-Bridge (strikt); Prod-Anfrage über das Backup auf
einem Dev-Worker (Herkunft `prod`, strikt); Koordinator fällt nach dem Anheften
aus (kein Neustart);
Kennung nur halb gesetzt; Bild-, Werkzeug-, Sitzungs- und Fremd-Tier-Anfragen
im Rückfall; Ledger-Zeile und Antwort tragen die Kennzeichnung.

Zusätzlicher Pyramiden-Prüfkontext: Default AUS; Header können die Kennung
nicht ersetzen; Prod mit versehentlich gesetztem Schalter bleibt strikt;
Rückfall ändert keine Pool-Reservierung; Rücknahme = Schalter entfernen und
Worker neu ausrollen.

Unit-Tests ohne Netz: `tests/unit/test_energy_staging_subscription_fallback.py`.
