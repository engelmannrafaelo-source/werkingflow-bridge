-- 072_test_ki_deckel.sql
--
-- Gedeckelte echte KI fuer Testkonten (Auftrag KD 91624155, 27.10.2026; R2 nach
-- Codex R1). Testkonten (tenants.account_type = 'test') von werking-works und
-- werking-report bekommen auf einer Bridge mit BRIDGE_TEST_KI_DECKEL=1 echte
-- Haiku-Aufrufe ueber den Direktweg, gedeckelt je Lauf (0,50 EUR), je App und
-- UTC-Tag (10 EUR) und gesamt je UTC-Tag (20 EUR). Logik:
-- src/budget/test_ki_deckel.py, einziger Schreibweg platform-api
-- (src/internal_routes.py, /v1/internal/test-ki-deckel/*).
--
-- test_ki_summe    — laufende Summen. ebene/app_id/schluessel:
--                    'lauf'   / App / 'YYYY-MM-DD|<user-uuid>|<X-Test-Lauf>'
--                    'tag'    / App / 'YYYY-MM-DD'
--                    'gesamt' / '*' / 'YYYY-MM-DD'
--                    geschlossen = true sperrt jede weitere Reservierung.
-- test_ki_buchung  — eine Zeile je Reservierung, reservierung_id kommt vom
--                    Worker (uuid4 je Aufruf). Primaerschluessel = Dedup: ein
--                    wiederholtes Reservieren oder Abrechnen derselben ID
--                    bucht nichts ein zweites Mal.
--
-- Forward-only, idempotent. Keine Bestandsdaten.

CREATE TABLE IF NOT EXISTS test_ki_summe (
    ebene        VARCHAR(8)     NOT NULL CHECK (ebene IN ('lauf', 'tag', 'gesamt')),
    app_id       VARCHAR(64)    NOT NULL,
    schluessel   VARCHAR(400)   NOT NULL,
    summe_eur    NUMERIC(12, 6) NOT NULL DEFAULT 0 CHECK (summe_eur >= 0),
    geschlossen  BOOLEAN        NOT NULL DEFAULT FALSE,
    aufrufe      INTEGER        NOT NULL DEFAULT 0,
    angelegt     TIMESTAMPTZ    NOT NULL DEFAULT now(),
    aktualisiert TIMESTAMPTZ    NOT NULL DEFAULT now(),
    PRIMARY KEY (ebene, app_id, schluessel)
);

CREATE TABLE IF NOT EXISTS test_ki_buchung (
    reservierung_id UUID           PRIMARY KEY,
    app_id          VARCHAR(64)    NOT NULL,
    tag             VARCHAR(10)    NOT NULL,
    lauf            VARCHAR(400)   NOT NULL,
    reserviert_eur  NUMERIC(12, 6) NOT NULL CHECK (reserviert_eur >= 0),
    ist_eur         NUMERIC(12, 6),
    status          VARCHAR(12)    NOT NULL DEFAULT 'offen'
                    CHECK (status IN ('offen', 'abgerechnet')),
    angelegt        TIMESTAMPTZ    NOT NULL DEFAULT now(),
    abgerechnet     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_test_ki_buchung_tag ON test_ki_buchung (tag, app_id);
