-- 051 — werking-check-Bestand nachziehen: Lizenz 'check-konto', keine Trial-Subscription
--        (Rafael, 2026-08-05; braucht 050 committed)
--
-- Zwei Aufraeumarbeiten fuer Konten, die im Fenster zwischen Migration 048
-- (werking-check existiert) und dem Register-Fix (identity/routes.py,
-- 2026-08-05) entstanden sind:
--
-- 1. Lizenz-Zeilen trugen den globalen Register-Default 'trial' →
--    'check-konto' (die inhaltlich richtige Aussage: regulaeres Konto).
-- 2. Der Report-Pfad legte ihnen eine 7-Tage-Trial-SUBSCRIPTION an (die
--    checkId-Ausnahme von 2026-07-30 griff nur am Check-Trichter, nicht am
--    Werkkonto). Diese Zeilen sind Fehl-Daten aus der Luecke, keine
--    Kundenhistorie: sie zeigten "Testphase" im Portal und hoben die
--    Check-Quota via Trial-Entitlement still auf die Plan-Quota. Loeschen
--    ist hier richtig — ECHTE Kaeufe (Credit-Packs) haben plan_id
--    'report-check-credit*' und sind nicht betroffen.
--
-- Forward-only, idempotent.

UPDATE app_licenses
   SET plan_id = 'check-konto'
 WHERE app_id = 'werking-check'
   AND plan_id = 'trial';

DELETE FROM subscriptions
 WHERE app_id = 'werking-check'
   AND plan_id = 'trial';
