-- 050 — plan_id enum: 'check-konto' als Lizenz-Plan des Werkkontos (Rafael, 2026-08-05)
--
-- Ein werking-check-Konto ist ein REGULAERES Konto, kein Trial: es gibt kein
-- Abo, das man nach einer Testphase kaufen muesste — man bekommt genau so
-- viele Checks, wie gebucht sind (plus freies Kontingent). Das bisherige
-- Lizenz-Etikett 'trial' (globaler Register-Default) war deshalb die falsche
-- Aussage (Portal: "Testphase" + Ablaufdatum).
--
-- 'check-konto' ist ein reiner LIZENZ-Wert (app_licenses.plan_id): bewusst
-- KEINE Zeile in der plans-Tabelle — nichts darf dagegen abrechnen, und ein
-- versehentliches get_plan('check-konto') schlaegt fail-loud fehl statt
-- still einen Topf zu erfinden. Verwendung folgt in identity/routes.py
-- (_REGISTER_LICENSE_PLAN_BY_APP) + Backfill in 051.
--
-- ALTER TYPE ... ADD VALUE: neuer Wert nicht in derselben Transaktion
-- verwendbar — daher 050/051-Split (gleiches Muster wie 040/041, 042/043,
-- 048/049). Forward-only, idempotent.

ALTER TYPE plan_id ADD VALUE IF NOT EXISTS 'check-konto';
