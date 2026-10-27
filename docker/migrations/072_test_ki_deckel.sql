-- 072_test_ki_deckel.sql
--
-- Gedeckelte echte KI fuer Testbenutzer (Auftrag KD 91624155, 27.10.2026).
-- Testkonten (tenants.account_type = 'test') der Apps werking-works und
-- werking-report duerfen auf einer Bridge mit BRIDGE_TEST_KI_DECKEL=1 echte
-- Haiku-Aufrufe machen, gedeckelt je Lauf (0,50 EUR) und je App und UTC-Tag
-- (10 EUR). Diese Tabelle haelt die laufenden Summen; Logik in
-- src/budget/test_ki_deckel.py.
--
--   ebene 'tag'  : schluessel = 'YYYY-MM-DD'                       (je App, UTC)
--   ebene 'lauf' : schluessel = 'YYYY-MM-DD|<user-uuid>|<X-Workflow-ID>'
--
-- summe_eur enthaelt Reservierungen (Schaetzung vor dem Aufruf) und wird nach
-- dem Aufruf auf die tatsaechlichen Kosten verrechnet. Eine nie verrechnete
-- Reservierung bleibt stehen — das zaehlt zu viel, nie zu wenig (fail-closed).
--
-- Forward-only, idempotent. Keine Bestandsdaten.

CREATE TABLE IF NOT EXISTS test_ki_deckel (
    app_id       VARCHAR(64)    NOT NULL,
    ebene        VARCHAR(8)     NOT NULL CHECK (ebene IN ('tag', 'lauf')),
    schluessel   VARCHAR(400)   NOT NULL,
    summe_eur    NUMERIC(12, 6) NOT NULL DEFAULT 0 CHECK (summe_eur >= 0),
    aufrufe      INTEGER        NOT NULL DEFAULT 0,
    angelegt     TIMESTAMPTZ    NOT NULL DEFAULT now(),
    aktualisiert TIMESTAMPTZ    NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, ebene, schluessel)
);

CREATE INDEX IF NOT EXISTS idx_test_ki_deckel_angelegt ON test_ki_deckel (angelegt);
