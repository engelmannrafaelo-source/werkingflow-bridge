-- 057 — Vorwarnung bei Monatsbudget-Verbrauch (Rafael, 2026-08-11)
--
-- Rafael hat für die anonymen Gratis-Checks 1000 EUR pro MONAT festgelegt und
-- ausdrücklich eine Vorwarnung verlangt: "ein Budget, von dem niemand merkt,
-- dass es zur Neige geht, ist keine Bremse, sondern eine Überraschung."
--
-- Idempotenz-Schlüssel ist (user_id, plan_id, period_reset_at, threshold_pct).
-- period_reset_at ist bewusst Teil des Schlüssels: der Monatsreset
-- (rollover_monthly_if_due, 868a1eb) schiebt resetAt weiter, damit entsteht
-- automatisch ein neuer Schlüssel — die Warnung ist in der neuen Periode wieder
-- scharf, OHNE dass jemand Stempel löschen muss. Eine Warnung je Schwelle je
-- Periode, garantiert durch den UNIQUE-Index, nicht durch Anwendungslogik.
--
-- Eigene Tabelle statt eines Feldes im monthly_budgets-JSON: das JSON wird an
-- mehreren Stellen mit `||` teil-gemergt (billing_service, budget/routes) —
-- ein Stempel darin wäre bei jedem Merge in Gefahr. Und Warnungen sind
-- Zustellhistorie, kein Budget-Zustand.

CREATE TABLE IF NOT EXISTS budget_warnings (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  plan_id          TEXT NOT NULL,
  -- Periodenanker aus monthly_budgets.resetAt. TEXT, weil der Wert dort als
  -- ISO-String liegt und exakt so verglichen werden muss (kein Cast-Drift).
  period_reset_at  TEXT NOT NULL,
  threshold_pct    INTEGER NOT NULL CHECK (threshold_pct > 0 AND threshold_pct <= 100),
  used_eur         NUMERIC(12,6) NOT NULL,
  limit_eur        NUMERIC(12,2) NOT NULL,
  recipient        TEXT NOT NULL,
  sent_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Der eigentliche Schutz gegen Doppelversand.
CREATE UNIQUE INDEX IF NOT EXISTS budget_warnings_einmal_je_periode
  ON budget_warnings (user_id, plan_id, period_reset_at, threshold_pct);

COMMENT ON TABLE budget_warnings IS
  'Zustellhistorie der Budget-Vorwarnungen an den Betreiber. Eine Zeile je '
  '(Konto, Plan, Periode, Schwelle) — der UNIQUE-Index ist die Idempotenz.';
