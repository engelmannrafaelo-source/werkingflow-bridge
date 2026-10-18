-- Vorpruefung zu Migration 070 (Abo-Invariante) — NUR LESEN.
--
-- Zeigt, ob und welche Abrechnungskonten (Nutzer + App) 070 blockieren
-- wuerden. Gibt es Treffer in Teil 1, bricht 070 laut ab; welches Abo bleibt,
-- entscheidet ein Mensch, nicht die Migration.
--
-- Die Bedingung ist DIESELBE wie im Bestands-DO-Block von 070: aktive
-- Mollie-Abos je Konto, ohne markierten Ersatz (ueberlappt_abo_id IS NULL),
-- mehr als eines. Vor 070 gibt es die Spalte noch nicht; 070 legt sie VOR der
-- Pruefung mit NULL an, also zaehlt dann jede Zeile als unmarkiert — die
-- Abfrage ohne Spalte ist dort gleichwertig. Ein erlaubter Ersatz neben
-- seinem Vorgaenger (offener Wechsel) blockiert nicht und steht in Teil 2.
--
-- Aufruf (Deploy-Sitzung, nicht automatisch):
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/070_vorpruefung_bestand.sql
-- READ ONLY, endet mit ROLLBACK. Liegt nicht unter docker/migrations, damit
-- bridge-migrate es nie als Migration einspielt. tests/billing liest die
-- markierten Abfragen und haelt sie gegen 070 (gleiches Urteil, gleiche Daten).

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL search_path = pg_catalog, public, pg_temp;

SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute
                WHERE attrelid = 'public.subscriptions'::regclass
                  AND attname = 'ueberlappt_abo_id' AND NOT attisdropped) AS hat_ersatzspalte \gset

\echo '== 070 bereits eingespielt (Ersatzspalte vorhanden)?' :hat_ersatzspalte

\echo '== Teil 1 — BLOCKIERT 070: Konten mit mehr als einem aktiven, unmarkierten Mollie-Abo'
\if :hat_ersatzspalte
-- BLOCKIERT-MIT-ERSATZSPALTE-BEGINN
SELECT s.user_id, s.app_id, count(*) AS aktive_mollie_abos,
       array_agg(s.id ORDER BY s.started_at)                     AS abo_ids,
       array_agg(s.plan_id::text ORDER BY s.started_at)          AS plaene,
       array_agg(s.mollie_subscription_id ORDER BY s.started_at) AS mollie_abos
  FROM public.subscriptions s
 WHERE s.status = 'active' AND s.mollie_subscription_id IS NOT NULL
   AND s.ueberlappt_abo_id IS NULL
 GROUP BY s.user_id, s.app_id
HAVING count(*) > 1
 ORDER BY s.user_id, s.app_id
-- BLOCKIERT-MIT-ERSATZSPALTE-ENDE
;
\else
-- BLOCKIERT-OHNE-ERSATZSPALTE-BEGINN
SELECT s.user_id, s.app_id, count(*) AS aktive_mollie_abos,
       array_agg(s.id ORDER BY s.started_at)                     AS abo_ids,
       array_agg(s.plan_id::text ORDER BY s.started_at)          AS plaene,
       array_agg(s.mollie_subscription_id ORDER BY s.started_at) AS mollie_abos
  FROM public.subscriptions s
 WHERE s.status = 'active' AND s.mollie_subscription_id IS NOT NULL
 GROUP BY s.user_id, s.app_id
HAVING count(*) > 1
 ORDER BY s.user_id, s.app_id
-- BLOCKIERT-OHNE-ERSATZSPALTE-ENDE
;
\endif

\echo '== Teil 1 Nutzer-E-Mails zu blockierenden Konten (zum Klaeren)'
\if :hat_ersatzspalte
SELECT DISTINCT s.user_id, u.email FROM public.subscriptions s LEFT JOIN public.users u ON u.id = s.user_id
 WHERE (s.user_id, s.app_id) IN (
     SELECT user_id, app_id FROM public.subscriptions
      WHERE status = 'active' AND mollie_subscription_id IS NOT NULL AND ueberlappt_abo_id IS NULL
      GROUP BY user_id, app_id HAVING count(*) > 1);
\else
SELECT DISTINCT s.user_id, u.email FROM public.subscriptions s LEFT JOIN public.users u ON u.id = s.user_id
 WHERE (s.user_id, s.app_id) IN (
     SELECT user_id, app_id FROM public.subscriptions
      WHERE status = 'active' AND mollie_subscription_id IS NOT NULL
      GROUP BY user_id, app_id HAVING count(*) > 1);
\endif

\echo '== Teil 2 — nur Info, blockiert nicht: laufende Wechsel (Vorgaenger + markierter Ersatz)'
\if :hat_ersatzspalte
SELECT n.user_id, n.app_id, n.ueberlappt_abo_id AS vorgaenger, n.id AS ersatz,
       ab.status AS wechsel_status, ab.doppelt_aktiv_seit
  FROM public.subscriptions n
  LEFT JOIN public.abo_abschluesse ab ON ab.neu_abo_id = n.id
 WHERE n.ueberlappt_abo_id IS NOT NULL
 ORDER BY n.user_id, n.app_id;
\else
\echo '(070 noch nicht eingespielt — es gibt keine Ersatzvermerke)'
\endif

\echo '== Teil 3 — nur Info: Konten mit Mollie-Abo UND weiteren aktiven Abos ohne Mollie (Testphase/Vergabe/manuell)'
SELECT user_id, app_id,
       count(*) FILTER (WHERE mollie_subscription_id IS NOT NULL) AS mollie,
       count(*) FILTER (WHERE mollie_subscription_id IS NULL)     AS ohne_mollie,
       array_agg(plan_id::text ORDER BY started_at)               AS plaene
  FROM public.subscriptions
 WHERE status = 'active'
 GROUP BY user_id, app_id
HAVING count(*) FILTER (WHERE mollie_subscription_id IS NOT NULL) >= 1
   AND count(*) FILTER (WHERE mollie_subscription_id IS NULL) >= 1
 ORDER BY user_id, app_id;

\echo '== Teil 4 — nur Info: offene Abo-Erstzahlungen der letzten 30 Tage'
SELECT p.user_id, p.plan_id, p.payment_id, p.created_at
  FROM public.pending_payments p
 WHERE p.type = 'subscription_first'
   AND NOT EXISTS (SELECT 1 FROM public.subscriptions s WHERE s.mollie_first_payment_id = p.payment_id)
   AND p.created_at > pg_catalog.now() - interval '30 days'
 ORDER BY p.created_at;

ROLLBACK;
