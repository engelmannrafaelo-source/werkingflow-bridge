-- Schema-Generator, Rafael 05.11.2026: eigenes internes Produkt/Abo.
-- Enum additions must commit before 075 uses them. No accounts/budgets touched.
ALTER TYPE app_id ADD VALUE IF NOT EXISTS 'werking-schema-generator';
ALTER TYPE plan_id ADD VALUE IF NOT EXISTS 'schema-generator';
