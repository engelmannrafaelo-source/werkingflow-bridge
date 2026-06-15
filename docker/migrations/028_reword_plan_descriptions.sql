-- 028_reword_plan_descriptions.sql
-- Reword customer-facing plan descriptions to professional copy.
-- The original seed (020_plans_table.sql) carried informal placeholder
-- text ("Danach EUR 250 oder weg.") that reads unprofessionally on the
-- public sortiment page. Forward-only correction; idempotent (UPDATE by id).
-- 020 is already applied + checksummed, so it must not be edited in place —
-- this migration supersedes its description values.

UPDATE plans
   SET description = '7 Tage kostenlos und unverbindlich testen — voller Funktionsumfang. Im Anschluss ins Standard-Abo wechseln; andernfalls endet der Zugang automatisch.'
 WHERE id = 'trial';

UPDATE plans
   SET description = 'Voller Funktionsumfang ohne Einschränkungen. KI-Budget inklusive, weitere Sitze zum selben Preis.'
 WHERE id = 'report-standard';
