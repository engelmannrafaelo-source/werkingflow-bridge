-- 042 — plan_id enum: add the Check-Credit price tiers (Preisstaffel).
--
-- Rafael 2026-07-30: sinnvolle Preisstaffel mit Mengenrabatt für den
-- WerkING-Check-Erstkauf. Die Bridge preist strikt linear (plan.price ×
-- quantity), Rabatt je Stufe braucht daher einen eigenen Plan mit eigenem
-- Stückpreis:
--   report-check-credit-1  → 1 Check  ×  9.00 EUR  =   9 EUR (Einzelkauf)
--   report-check-credit-5  → 5 Checks ×  7.00 EUR  =  35 EUR (−22 %)
--   report-check-credit    → 20 Checks × 5.00 EUR  = 100 EUR (−44 %, bestehend)
--
-- Split in eine EIGENE Migration: ALTER TYPE ... ADD VALUE darf nicht in der
-- Transaktion verwendet werden, die es hinzugefügt hat — der INSERT folgt in
-- 043 (gleiches Muster wie 040/041).
--
-- Forward-only, idempotent.

ALTER TYPE plan_id ADD VALUE IF NOT EXISTS 'report-check-credit-5';
ALTER TYPE plan_id ADD VALUE IF NOT EXISTS 'report-check-credit-1';
