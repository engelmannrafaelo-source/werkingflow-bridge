-- 040 — plan_id enum: add 'report-check-credit'.
--
-- Self-Service-Erstkauf-Checkout für das WerkING-Check Credit-Pack (Rafael,
-- Entscheidung 2026-07-28). Ein Credit = eine Check-Freischaltung (Word-
-- Downloads). App ruft den Checkout mit quantity=20 (100 EUR / 20 Checks).
--
-- Split in eine EIGENE Migration: ALTER TYPE ... ADD VALUE darf laut Postgres
-- nicht in derselben Transaktion verwendet werden, in der es hinzugefügt
-- wurde ("unsafe use of new value of enum type"). bridge-migrate.sh wrapt
-- jede Migrationsdatei in eine einzelne Transaktion — der INSERT INTO plans
-- mit diesem neuen Wert folgt daher in einer separaten, späteren Migration
-- (041), analog zum bereits durchlaufenen 'project_pack'-Fall (siehe 029).
--
-- Forward-only, idempotent.

ALTER TYPE plan_id ADD VALUE IF NOT EXISTS 'report-check-credit';
