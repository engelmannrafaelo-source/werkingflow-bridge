-- Vorpruefung zu Migration 070 (Abo-Invariante) — NUR LESEN.
--
-- Zeigt, ob und welche Abrechnungskonten (Nutzer + App) heute mehr als ein
-- aktives Mollie-Abo haben. Gibt es Treffer in Teil 1, bricht 070 laut ab;
-- welches Abo bleibt, entscheidet ein Mensch, nicht die Migration.
--
-- Aufruf (Deploy-Sitzung, nicht automatisch):
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/070_vorpruefung_bestand.sql
-- Die Transaktion ist READ ONLY und endet mit ROLLBACK: das Skript kann
-- nichts schreiben. Liegt nicht unter docker/migrations, damit bridge-migrate
-- es nie als Migration einspielt.

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

\echo '== 070 bereits eingespielt? (abo_abschluesse vorhanden = t)'
SELECT to_regclass('public.abo_abschluesse') IS NOT NULL AS abo_abschluesse_vorhanden;

\echo '== Teil 1 — BLOCKIERT 070: Konten mit mehr als einem aktiven Mollie-Abo'
SELECT s.user_id, u.email, s.app_id, count(*) AS aktive_mollie_abos,
       array_agg(s.id ORDER BY s.started_at)                     AS abo_ids,
       array_agg(s.plan_id::text ORDER BY s.started_at)          AS plaene,
       array_agg(s.mollie_subscription_id ORDER BY s.started_at) AS mollie_abos,
       array_agg(s.started_at ORDER BY s.started_at)             AS begonnen
  FROM subscriptions s
  LEFT JOIN users u ON u.id = s.user_id
 WHERE s.status = 'active' AND s.mollie_subscription_id IS NOT NULL
 GROUP BY s.user_id, u.email, s.app_id
HAVING count(*) > 1
 ORDER BY s.user_id, s.app_id;

\echo '== Teil 1 als Zahl (0 = 070 laeuft durch)'
SELECT count(*) AS blockierende_konten FROM (
    SELECT 1 FROM subscriptions
     WHERE status = 'active' AND mollie_subscription_id IS NOT NULL
     GROUP BY user_id, app_id HAVING count(*) > 1) d;

\echo '== Teil 2 — nur Info, blockiert nicht: Konten mit Mollie-Abo UND weiteren aktiven Abos (Testphase/Vergabe/manuell)'
SELECT user_id, app_id,
       count(*) FILTER (WHERE mollie_subscription_id IS NOT NULL) AS mollie,
       count(*) FILTER (WHERE mollie_subscription_id IS NULL)     AS ohne_mollie,
       array_agg(plan_id::text ORDER BY started_at)               AS plaene
  FROM subscriptions
 WHERE status = 'active'
 GROUP BY user_id, app_id
HAVING count(*) FILTER (WHERE mollie_subscription_id IS NOT NULL) >= 1
   AND count(*) > 1
 ORDER BY user_id, app_id;

\echo '== Teil 3 — nur Info: offene Abo-Erstzahlungen (bekommen beim Webhook einen Abschluss nachgetragen)'
SELECT p.user_id, p.plan_id, p.payment_id, p.created_at
  FROM pending_payments p
 WHERE p.type = 'subscription_first'
   AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.mollie_first_payment_id = p.payment_id)
   AND p.created_at > NOW() - interval '30 days'
 ORDER BY p.created_at;

ROLLBACK;
