# Ledger: Finanzierungsquelle und Pool-Buchung ohne Mandant

Entscheid KD 03.11.2026. Keine Migration, keine Umbuchung bestehender Zeilen.

## Vertrag

1. **Finanzierungsquelle in der Zeile.** Eine Ledger-Zeile, deren Aufruf aus dem
   Promotion-Credit-Pool bezahlt wurde, trägt in `provider_metadata`:
   `funding_policy` (`promotional_only`), `automatic_paid_fallback` (false) und
   die Reservierungen des Koordinators. Es gibt zwei Formen:
   `credit_pool_reservation` für einen einzelnen Aufruf (Chat direkt, Vision)
   und `credit_pool_reservations` mit `credit_pool_grants` für eine Zeile über
   viele Aufrufe (Recherche-Lauf mit Runden und Lückenprüfer, Dokument-Vision).
   Neue Zeilen nennen keine Konto-ID, keine Organisation und keinen Schlüssel.
   Bestehende Chat-Zeilen behalten `credit_pool_account` und `credit_pool_org`
   unverändert (siehe Offene Punkte). Recherche-Zeilen eines Job-Selbstaufrufs
   tragen zusätzlich `bridge_job_id`.
2. **Echtkosten.** `resolve_ledger_cost` bucht `research-cloud` mit 0 als
   `real_cost_eur`, wenn die Metadaten des Aufrufs selbst den Pool belegen:
   `funding_policy == promotional_only` **und** mindestens eine Reservierungs-ID
   (`funding_policy.pool_funded`). `hypothetical_cost_eur` behält den
   Listenpreis. App-Name, Header oder der Policy-Marker allein reichen nicht aus.
   Für alle anderen Fälle bleibt alles wie bisher: Recherche mit bezahltem
   Schlüssel, Bedrock, Vertex, `pay_per_token`-Mandanten und alle übrigen
   Provider.
3. **Pool-Buchung ohne Mandant.** Hat der Nutzer keine `users⋈tenants`-Zeile
   (zum Beispiel Mandant `phasentest`), wurde die Zeile bisher übersprungen
   (`skipped:no_tenant`). Ein Aufruf, dessen Metadaten eine
   Pool-Reservierung nennen, wird jetzt unter der anonymen Identität aus
   Migration 032 geschrieben. Deren Mandant wird über denselben Endpunkt
   aufgelöst und nicht im Code festgelegt. Die Zeile trägt
   `no_tenant: true`, `no_tenant_user_id` und `no_tenant_reason`; im
   Audit-Payload steht `noTenant`. Es gibt keinen Budgetabzug. Fehlt die
   anonyme Identität, bleibt die Zeile im Spool offen (`failed`) und wird nicht
   übersprungen. Tenantlose Aufrufe ohne Pool-Nachweis werden weiter
   übersprungen.
4. **Dokument-Vision bei Fehler.** Scheitert eine Dokumentkonvertierung, nachdem
   Bildaufrufe über den Pool gelaufen sind (Frist, Abbruch, 502, 409), werden
   diese Aufrufe trotzdem gebucht: Status `success`, `document_status: error`,
   Reservierungen. Danach folgt wie bisher die docling-Fehlerzeile.
5. **Heimat-Bridge (ADR-0011).** Ein Aufruf mit fremder Herkunft wird schon
   heute in das Ledger der Heimat-Bridge geschrieben (`ledger_client` →
   `call_platform(domain="user")`; der Spool stellt die Herkunft beim Replay
   wieder her). Neu tragen solche Zeilen `bridge_origin` und
   `bridge_executed_on` (`<bridge>/<worker>`). Zeilen eigener Herkunft bleiben
   unverändert.
6. Der Nutzeraufruf bricht nie an der Buchung. Jeder Fehler bleibt „offen im
   Spool“ oder wird geloggt.

7. **Abo-Bildweg und Abo-Modus** (`energy-abo-bildweg.md`). Zeilen auf dem
   Abo tragen `funding_policy=subscription_staging_fallback`,
   `funding_source=subscription` und `pool_umgangen` (true im Abo-Modus) und
   keine Pool-Reservierung. Bildzeilen haben die Fahrspur
   `vision_subscription`. `real_cost_eur` bleibt bei der bestehenden
   Abo-Regel. Das erweitert die Felder von Punkt 1, ohne sie umzudeuten.

## Grenzen

- Ein Recherche-Lauf ergibt eine Zeile und N Reservierungen. Die Anzahl der
  Pool-Buchungen ist daher nie gleich der Anzahl der Ledger-Zeilen. Der Abgleich
  läuft über `provider_metadata->'credit_pool_reservations'` gegen
  `anthropic_credit_reservations.id`.
- Erfasst wird jede beim Koordinator committete Reservierung, also auch
  freigegebene und „unknown“. Ihren Zustand liefert die Pool-Tabelle.
- Fehlerzeilen der Recherche buchen weiter 0 Token. Die Kosten der Runden vor
  dem Fehler stehen nur über die Reservierungen im Pool.

## Pyramiden-Szenarien

Recherche mit drei Pool-Runden für einen Nutzer ohne Mandant: genau eine Zeile
mit allen Reservierungen, `workflow_id`, `bridge_job_id`, real 0 und
hypothetical über 0. Dieselbe Recherche für einen Mandanten mit `subscription`:
Zeile beim Mandanten, real 0. Recherche auf dem bezahlten Schlüssel: real gleich
hypothetical, keine Pool-Felder. Chat direkt über den Pool ohne Mandant: Zeile
unter der anonymen Identität, kein Abzug. CLI-Aufruf ohne Mandant: weiterhin
`skipped:no_tenant`. Anonyme Identität fehlt: Zeile bleibt offen. Platform-API
nicht erreichbar: Zeile bleibt offen, und der Aufruf antwortet trotzdem.
Dokument mit Frist-504 nach Triage und einer Beschreibung: zwei Pool-Aufrufe
gebucht plus die docling-Fehlerzeile. Prod-Aufruf auf einem Dev-Worker: Zeile im
Prod-Ledger mit `bridge_origin=prod` und `bridge_executed_on=dev/<worker>`.

Unit-Tests ohne Netz: `tests/unit/test_ledger_funding_source.py`.
`tests/billing/test_ledger_real_cost.py` bleibt unverändert (Hausregel).

## Offene Punkte

- Chat- und Vision-Zeilen tragen weiter `credit_pool_account` und
  `credit_pool_org`. Ob sie entfallen sollen, muss die KD entscheiden; dann
  wäre `credit_pool.post_messages`-Meta beim Buchen zu filtern.
- Ein eigener Mandant `bridge-no-tenant` statt der anonymen Identität bräuchte
  eine Migration (INSERT tenants/users). Das ist nur ein Vorschlag und nicht
  gebaut.
- `FederationMisconfigured` (fremde Herkunft ohne Peer): Die Zeile bleibt im
  Spool des ausführenden Workers. Nach `BRIDGE_LEDGER_SPOOL_MAX_ATTEMPTS` bzw.
  `MAX_AGE_S` wird sie mit Log begraben. In der Heimat ist sie dann nicht
  sichtbar.

### Offene Punkte aus der Zweitprüfung (nicht gebaut)

1. **Nur der Fall „UUID ohne Mandant“ ist geschlossen.** `skipped:no_user`,
   `skipped:email_unknown` und `skipped:not_a_user` überspringen die Zeile
   weiter, auch wenn der Aufruf über den Pool lief.
2. **Abbruch mitten im Lauf.** Bei `CancelledError` (Aufrufer weg, Job-Stopp,
   Worker-Ende) während eines Research-cloud-Laufs wird `persist_ai_call_activity`
   nie erreicht. Es entsteht keine Zeile, die Reservierungen stehen nur in der
   Pool-Tabelle.
3. **Fehlerzeilen kosten 0.** Research-Fehler- und Modellabbruchzeilen mit
   Status `error` haben `hypothetical_cost_eur = 0`. Die Pool-Kosten der Runden
   davor stehen nur über die Reservierungen.
4. **Lücken bei der Dokumentkonvertierung.** Nicht abgedeckt sind:
   - der Privacy-Dienst-Pfad: `vision_usage` wird nur bei Status < 400
     übernommen;
   - der Proxy-409 bei fehlender Attestierung;
   - `/v1/document/convert-and-anonymize`.

   Außerdem erzeugen die jetzt gebuchten Vision-Zeilen eines gescheiterten
   Dokuments (Status `success`) bei einem Mandanten mit Plan einen
   **Budgetabzug für ein Dokument, das der Kunde nicht erhalten hat**. Ob das
   gewollt ist, entscheidet die KD (Alternative: diese Zeilen mit
   `billing_account` intern buchen oder den Abzug auslassen).
5. **`_hop_meta` beim Replay.** Beim Spool-Replay wird die Herkunft aus dem
   Datensatz wiederhergestellt, `bridge_executed_on` stammt aber aus dem
   *replayenden* Prozess. Der Wert wird nicht im Spool-Datensatz
   eingefroren. Meist ist das derselbe Worker, sicher ist es nicht.
6. **`no_tenant_user_id` steht nur in der Meta.** `user_id` der Zeile ist die
   anonyme Identität. Abfragen je Nutzer, Budget und Plattform-Admin-Ansichten
   sehen den echten Nutzer nicht, nur `provider_metadata->>'no_tenant_user_id'`.

Gehärtet nach der Zweitprüfung: `bridge_job_id` wird nur übernommen, wenn der
Header `X-Bridge-Job-Lauf` vollständig der Job-ID-Grammatik entspricht
(`src/jobs/job_id.py`, getaggt oder alt, höchstens 64 Zeichen). Andere Werte
werden mit Log verworfen. `no_tenant*` aus der Call-Meta wird vom Writer
überschrieben, weil sein Marker zuletzt gemischt wird. Ist ein Mandant
vorhanden, wird dieses Feld nicht gesetzt; die Call-Meta stammt dort nur aus
Bridge-Code.
