-- 070 — Abo-Invariante je Abrechnungskonto (Nutzer + App).
--
-- Codex-Pruefung 18.10.2026 zu 069 (Planwechsel): vier Wege zu zwei oder null
-- abrechenbaren Abos — zwei offene Wechsel ergeben zwei neue Abos, ein
-- dauerhafter Kuendigungsfehler laesst alt+neu laufen, ein spaeter Retry
-- kuendigt nach zwischenzeitlicher Kuendigung des Ersatzes das letzte Abo,
-- und zwei gleichzeitige Webhooks derselben Zahlung legen bei Mollie ein
-- zweites (in keiner Zeile erfasstes) Abo an.
--
-- Statt Einzelfaellen liegt die Invariante jetzt in der Datenbank:
--
--   Je Abrechnungskonto hoechstens EIN aktives Mollie-Abo — waehrend eines
--   laufenden Wechsels zusaetzlich genau ein als Ersatz markiertes.
--   Hoechstens EIN offener Abschluss (Erstkauf oder Wechsel) je Konto.
--
-- "Mollie-Abo" = Zeile mit mollie_subscription_id (nur die bezahlte
-- Erstzahlung legt eine an). Testphasen, Operator-Vergaben und manuelle
-- Bestellungen tragen keine und zaehlen nicht: sie rechnen nicht wiederkehrend
-- ab. Konto = (user_id, app_id): Plaene gehoeren je einer App.
--
-- Bestand: gibt es schon ein Konto mit zwei aktiven Mollie-Abos, bricht die
-- Migration LAUT ab (kein stilles Bereinigen — welches Abo bleibt, entscheidet
-- ein Mensch). Abfrage dafuer steht in der Fehlermeldung.

-- ---------------------------------------------------------------------------
-- 1. Abschluss-Vorgaenge: Erstkauf und Planwechsel, je Erstzahlung genau einer
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS abo_abschluesse (
    id                   UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID          NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    app_id               app_id        NOT NULL,
    plan_id              plan_id       NOT NULL,
    seats                INTEGER       NOT NULL CHECK (seats > 0),
    -- Wechsel: das beim Checkout gemerkte, zu ersetzende Abo. NULL = Erstkauf.
    alt_abo_id           UUID          REFERENCES subscriptions(id) ON DELETE RESTRICT,
    -- Mollie-Erstzahlung; NULL nur kurz zwischen Reservierung und Checkout.
    payment_id           VARCHAR(128)  UNIQUE,
    neu_abo_id           UUID          UNIQUE REFERENCES subscriptions(id) ON DELETE RESTRICT,
    status               VARCHAR(20)   NOT NULL,
    -- Ausgang eines abgeschlossenen Wechsels (alt_gekuendigt,
    -- alt_schon_beendet, ersatz_nicht_aktiv, erstkauf).
    ergebnis             VARCHAR(40),
    kuendigung_versuche  INTEGER       NOT NULL DEFAULT 0,
    letzter_fehler       TEXT,
    doppelt_aktiv_seit   TIMESTAMPTZ,
    angelegt_am          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    geaendert_am         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT abo_abschluesse_status_ck CHECK (status IN (
        'reserviert',     -- Platz belegt, Mollie-Checkout wird angelegt
        'offen',          -- Checkout angelegt, Zahlung offen
        'neu_aktiv',      -- neues Abo aktiv, Kuendigung des alten steht aus
        'doppelt_aktiv',  -- Kuendigung des alten gescheitert: ALARM, Nachholauftrag
        'abgeschlossen',
        'abgebrochen',    -- Zahlung gescheitert / Checkout nicht angelegt
        'verworfen',      -- Reservierung ohne Checkout verfallen
        'konflikt'        -- bezahlt, aber nicht aktivierbar: ALARM, Mensch
    )),
    CONSTRAINT abo_abschluesse_offen_hat_zahlung_ck
        CHECK (status <> 'offen' OR payment_id IS NOT NULL),
    CONSTRAINT abo_abschluesse_wechsel_hat_beide_ck
        CHECK (status NOT IN ('neu_aktiv', 'doppelt_aktiv')
               OR (neu_abo_id IS NOT NULL AND alt_abo_id IS NOT NULL)),
    CONSTRAINT abo_abschluesse_abgeschlossen_hat_neu_ck
        CHECK (status <> 'abgeschlossen' OR neu_abo_id IS NOT NULL),
    CONSTRAINT abo_abschluesse_doppelt_hat_zeit_ck
        CHECK (status <> 'doppelt_aktiv' OR doppelt_aktiv_seit IS NOT NULL)
);

-- Hoechstens EIN offener Abschluss je Konto. Ein zweiter Wechsel (oder ein
-- Erstkauf neben einem offenen Wechsel) scheitert hier — auch bei zwei
-- gleichzeitigen Anfragen.
CREATE UNIQUE INDEX IF NOT EXISTS abo_abschluesse_ein_offener_je_konto
    ON abo_abschluesse (user_id, app_id)
    WHERE status IN ('reserviert', 'offen', 'neu_aktiv', 'doppelt_aktiv');

CREATE INDEX IF NOT EXISTS abo_abschluesse_nachholen
    ON abo_abschluesse (geaendert_am)
    WHERE status IN ('neu_aktiv', 'doppelt_aktiv');

-- ---------------------------------------------------------------------------
-- 2. Ersatz-Vermerk an der Abo-Zeile
-- ---------------------------------------------------------------------------
ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS ueberlappt_abo_id UUID REFERENCES subscriptions(id) ON DELETE RESTRICT;
ALTER TABLE subscriptions DROP CONSTRAINT IF EXISTS subscriptions_ueberlappt_nicht_selbst_ck;
ALTER TABLE subscriptions
    ADD CONSTRAINT subscriptions_ueberlappt_nicht_selbst_ck
    CHECK (ueberlappt_abo_id IS NULL OR ueberlappt_abo_id <> id);

-- Der Vermerk darf nur auf ein aktives Mollie-Abo DESSELBEN Kontos zeigen,
-- das selbst kein Ersatz ist (keine Ketten).
CREATE OR REPLACE FUNCTION subscriptions_ueberlappt_pruefen() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    ziel RECORD;
BEGIN
    IF NEW.ueberlappt_abo_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT user_id, app_id, status, mollie_subscription_id, ueberlappt_abo_id
      INTO ziel FROM subscriptions WHERE id = NEW.ueberlappt_abo_id;
    IF NOT FOUND
       OR ziel.user_id <> NEW.user_id OR ziel.app_id <> NEW.app_id
       OR ziel.status <> 'active' OR ziel.mollie_subscription_id IS NULL
       OR ziel.ueberlappt_abo_id IS NOT NULL THEN
        RAISE EXCEPTION 'subscriptions.ueberlappt_abo_id % zeigt nicht auf ein aktives, unmarkiertes Mollie-Abo desselben Kontos', NEW.ueberlappt_abo_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS subscriptions_ueberlappt_pruefen ON subscriptions;
CREATE TRIGGER subscriptions_ueberlappt_pruefen
    BEFORE INSERT OR UPDATE OF ueberlappt_abo_id ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION subscriptions_ueberlappt_pruefen();

-- Verlaesst das ersetzte Abo den Status 'active' (Wechsel-Kuendigung, Kunde,
-- Sperre), verliert der Ersatz seinen Vermerk und belegt den einen regulaeren
-- Platz. So bleibt kein Ersatz dauerhaft am Index vorbei.
CREATE OR REPLACE FUNCTION subscriptions_ueberlappung_aufloesen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'active' AND NEW.status <> 'active' THEN
        UPDATE subscriptions SET ueberlappt_abo_id = NULL
         WHERE ueberlappt_abo_id = NEW.id;
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS subscriptions_ueberlappung_aufloesen ON subscriptions;
CREATE TRIGGER subscriptions_ueberlappung_aufloesen
    AFTER UPDATE OF status ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION subscriptions_ueberlappung_aufloesen();

-- ---------------------------------------------------------------------------
-- 3. Bestand pruefen, dann die Invariante als Index
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    n INTEGER;
BEGIN
    SELECT count(*) INTO n FROM (
        SELECT 1 FROM subscriptions
         WHERE status = 'active' AND mollie_subscription_id IS NOT NULL
         GROUP BY user_id, app_id HAVING count(*) > 1
    ) d;
    IF n > 0 THEN
        RAISE EXCEPTION '070: % Konto/Konten mit mehr als einem aktiven Mollie-Abo — erst von Hand klaeren: SELECT user_id, app_id, array_agg(id) FROM subscriptions WHERE status=''active'' AND mollie_subscription_id IS NOT NULL GROUP BY 1,2 HAVING count(*)>1', n;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_ein_mollie_abo_je_konto
    ON subscriptions (user_id, app_id)
    WHERE status = 'active' AND mollie_subscription_id IS NOT NULL
      AND ueberlappt_abo_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_hoechstens_ein_ersatz_je_konto
    ON subscriptions (user_id, app_id)
    WHERE status = 'active' AND mollie_subscription_id IS NOT NULL
      AND ueberlappt_abo_id IS NOT NULL;
