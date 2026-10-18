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
CREATE TABLE IF NOT EXISTS public.abo_abschluesse (
    id                   UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID          NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    app_id               app_id        NOT NULL,
    plan_id              plan_id       NOT NULL,
    seats                INTEGER       NOT NULL CHECK (seats > 0),
    -- Wechsel: das beim Checkout gemerkte, zu ersetzende Abo. NULL = Erstkauf.
    alt_abo_id           UUID          REFERENCES public.subscriptions(id) ON DELETE RESTRICT,
    -- Mollie-Erstzahlung; NULL nur kurz zwischen Reservierung und Checkout.
    payment_id           VARCHAR(128)  UNIQUE,
    neu_abo_id           UUID          UNIQUE REFERENCES public.subscriptions(id) ON DELETE RESTRICT,
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
    ON public.abo_abschluesse (user_id, app_id)
    WHERE status IN ('reserviert', 'offen', 'neu_aktiv', 'doppelt_aktiv');

CREATE INDEX IF NOT EXISTS abo_abschluesse_nachholen
    ON public.abo_abschluesse (geaendert_am)
    WHERE status IN ('neu_aktiv', 'doppelt_aktiv');

-- ---------------------------------------------------------------------------
-- 2. Ersatz-Vermerk an der Abo-Zeile
-- ---------------------------------------------------------------------------
ALTER TABLE public.subscriptions
    ADD COLUMN IF NOT EXISTS ueberlappt_abo_id UUID REFERENCES public.subscriptions(id) ON DELETE RESTRICT;
ALTER TABLE public.subscriptions DROP CONSTRAINT IF EXISTS subscriptions_ueberlappt_nicht_selbst_ck;
ALTER TABLE public.subscriptions
    ADD CONSTRAINT subscriptions_ueberlappt_nicht_selbst_ck
    CHECK (ueberlappt_abo_id IS NULL OR ueberlappt_abo_id <> id);

-- Der Vermerk darf nur auf ein aktives Mollie-Abo DESSELBEN Kontos zeigen,
-- das selbst kein Ersatz ist (keine Ketten).
CREATE OR REPLACE FUNCTION public.subscriptions_ueberlappt_pruefen() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    ziel RECORD;
BEGIN
    IF NEW.ueberlappt_abo_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT user_id, app_id, status, mollie_subscription_id, ueberlappt_abo_id
      INTO ziel FROM public.subscriptions WHERE id = NEW.ueberlappt_abo_id;
    IF NOT FOUND
       OR ziel.user_id <> NEW.user_id OR ziel.app_id <> NEW.app_id
       OR ziel.status <> 'active' OR ziel.mollie_subscription_id IS NULL
       OR ziel.ueberlappt_abo_id IS NOT NULL THEN
        RAISE EXCEPTION 'subscriptions.ueberlappt_abo_id % zeigt nicht auf ein aktives, unmarkiertes Mollie-Abo desselben Kontos', NEW.ueberlappt_abo_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS subscriptions_ueberlappt_pruefen ON public.subscriptions;
CREATE TRIGGER subscriptions_ueberlappt_pruefen
    BEFORE INSERT OR UPDATE ON public.subscriptions
    FOR EACH ROW EXECUTE FUNCTION public.subscriptions_ueberlappt_pruefen();

-- Verlaesst das ersetzte Abo den Status 'active' (Wechsel-Kuendigung, Kunde,
-- Sperre), verliert der Ersatz seinen Vermerk und belegt den einen regulaeren
-- Platz. So bleibt kein Ersatz dauerhaft am Index vorbei.
CREATE OR REPLACE FUNCTION public.subscriptions_ueberlappung_aufloesen() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF OLD.status = 'active' AND NEW.status <> 'active' THEN
        UPDATE public.subscriptions SET ueberlappt_abo_id = NULL
         WHERE ueberlappt_abo_id = NEW.id;
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS subscriptions_ueberlappung_aufloesen ON public.subscriptions;
CREATE TRIGGER subscriptions_ueberlappung_aufloesen
    AFTER UPDATE ON public.subscriptions
    FOR EACH ROW EXECUTE FUNCTION public.subscriptions_ueberlappung_aufloesen();

-- Namensaufloesung (Codex R4): jede Funktion dieser Migration laeuft mit
-- festem search_path = pg_catalog, public, pg_temp (pg_temp ausdruecklich
-- zuletzt) und nennt Tabellen, Funktionen und Typen voll qualifiziert. Eine
-- temporaere Tabelle "subscriptions"/"abo_abschluesse" einer Sitzung kann die
-- Pruefungen damit nicht mehr taeuschen. Trigger sind an keine Spaltenliste
-- gebunden: sie feuern auf jede Schreibung der Tabelle.

-- ---------------------------------------------------------------------------
-- 2b. Die Invariante unabhaengig vom Codepfad (Codex-Pruefung R2)
-- ---------------------------------------------------------------------------
-- Indizes und Sofort-Trigger allein reichen nicht: ein gleichzeitiges Beenden
-- des Vorgaengers sah einen noch nicht committeten Ersatz nicht, und direkte
-- UPDATEs an Konto- oder Mollie-Feldern pruefte niemand nach. Deshalb:
--   (a) Kontofelder sind unveraenderlich (user_id, app_id; mollie_subscription_id
--       sobald gesetzt) — ein Abo wechselt nie Besitzer, App oder Mollie-Abo;
--   (b) jede Aenderung an einem Mollie-Abo oder Abschluss sperrt das Konto
--       (pg_advisory_xact_lock, gleicher Schluessel wie der Code) — Schreiber
--       eines Kontos laufen nacheinander, auch per Hand-SQL;
--   (c) schliesst ein Abschluss, verliert sein Ersatz den Vermerk in DERSELBEN
--       Transaktion;
--   (d) zum Commit prueft ein zurueckgestellter Constraint-Trigger das ganze
--       Konto: hoechstens ein regulaeres aktives Mollie-Abo, hoechstens ein
--       Ersatz, und jeder Ersatz nur, solange ein offener Wechsel (neu_aktiv /
--       doppelt_aktiv) genau dieses Paar traegt.
-- Wegen (b) sieht die Pruefung (d) unter READ COMMITTED den committeten Stand
-- jedes frueheren Schreibers. REPEATABLE READ wird abgewiesen: dessen
-- Schnappschuss kann vor der Sperre liegen.

CREATE OR REPLACE FUNCTION public.abo_konto_schluessel(p_user UUID, p_app public.app_id) RETURNS bigint
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $$
    -- Identisch mit billing_service._konto_sperrschluessel (hashtext des Textes).
    SELECT hashtext('abo-konto:' || p_user::text || ':' || p_app::text)::bigint
$$;

-- Positivliste der Isolation (Codex R3): schreibende Transaktionen auf Abos
-- und Abschluessen NUR unter READ COMMITTED. Jede andere Stufe haelt einen
-- Schnappschuss, der vor der Kontosperre liegen kann (REPEATABLE READ und
-- SERIALIZABLE neben READ-COMMITTED-Schreibern gemessen) — Aufloesungs-Trigger
-- und Commit-Pruefung saehen dann nicht den aktuellen Stand.
CREATE OR REPLACE FUNCTION public.abo_nur_read_committed() RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'Abo-/Abschlussaenderungen nur unter READ COMMITTED (hier: %)',
            current_setting('transaction_isolation') USING ERRCODE = 'check_violation';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.abo_konto_sperren(p_user UUID, p_app public.app_id) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.abo_nur_read_committed();
    PERFORM pg_advisory_xact_lock(public.abo_konto_schluessel(p_user, p_app));
END $$;

-- (a)+(b) fuer subscriptions. Name beginnt mit "abo_00": BEFORE-Trigger laufen
-- in Namensreihenfolge, die Sperre also vor subscriptions_ueberlappt_pruefen.
CREATE OR REPLACE FUNCTION public.abo_00_subscriptions_sperre() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.abo_nur_read_committed();   -- jede Zeile, auch ohne Mollie-Abo
    IF TG_OP = 'UPDATE' THEN
        IF NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.app_id IS DISTINCT FROM OLD.app_id THEN
            RAISE EXCEPTION 'subscriptions %: user_id/app_id sind unveraenderlich', OLD.id
                USING ERRCODE = 'check_violation';
        END IF;
        IF OLD.mollie_subscription_id IS NOT NULL
           AND NEW.mollie_subscription_id IS DISTINCT FROM OLD.mollie_subscription_id THEN
            RAISE EXCEPTION 'subscriptions %: mollie_subscription_id ist gesetzt und unveraenderlich', OLD.id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.mollie_subscription_id IS NOT NULL OR OLD.ueberlappt_abo_id IS NOT NULL THEN
            PERFORM public.abo_konto_sperren(OLD.user_id, OLD.app_id);
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.mollie_subscription_id IS NOT NULL OR NEW.ueberlappt_abo_id IS NOT NULL
       OR (TG_OP = 'UPDATE' AND OLD.ueberlappt_abo_id IS NOT NULL) THEN
        PERFORM public.abo_konto_sperren(NEW.user_id, NEW.app_id);
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS abo_00_subscriptions_sperre ON public.subscriptions;
CREATE TRIGGER abo_00_subscriptions_sperre
    BEFORE INSERT OR DELETE OR UPDATE
    ON public.subscriptions
    FOR EACH ROW EXECUTE FUNCTION public.abo_00_subscriptions_sperre();

-- (a)+(b) fuer abo_abschluesse.
CREATE OR REPLACE FUNCTION public.abo_00_abschluesse_sperre() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.abo_nur_read_committed();
    IF TG_OP = 'UPDATE' AND (NEW.user_id IS DISTINCT FROM OLD.user_id
                             OR NEW.app_id IS DISTINCT FROM OLD.app_id
                             OR NEW.alt_abo_id IS DISTINCT FROM OLD.alt_abo_id
                             OR (OLD.neu_abo_id IS NOT NULL AND NEW.neu_abo_id IS DISTINCT FROM OLD.neu_abo_id)) THEN
        RAISE EXCEPTION 'abo_abschluesse %: Konto, altes und (gesetztes) neues Abo sind unveraenderlich', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        PERFORM public.abo_konto_sperren(OLD.user_id, OLD.app_id);
        RETURN OLD;
    END IF;
    PERFORM public.abo_konto_sperren(NEW.user_id, NEW.app_id);
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS abo_00_abschluesse_sperre ON public.abo_abschluesse;
CREATE TRIGGER abo_00_abschluesse_sperre
    BEFORE INSERT OR UPDATE OR DELETE ON public.abo_abschluesse
    FOR EACH ROW EXECUTE FUNCTION public.abo_00_abschluesse_sperre();

-- (c) Abschluss verlaesst neu_aktiv/doppelt_aktiv (oder wird geloescht):
-- Ersatzvermerk in derselben Transaktion aufloesen. Laeuft das alte Abo dann
-- noch, scheitert das sofort am Index bzw. spaetestens zum Commit an (d).
CREATE OR REPLACE FUNCTION public.abo_abschluss_ersatz_aufloesen() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF OLD.status IN ('neu_aktiv', 'doppelt_aktiv') AND OLD.neu_abo_id IS NOT NULL
       AND (TG_OP = 'DELETE' OR NEW.status NOT IN ('neu_aktiv', 'doppelt_aktiv')) THEN
        UPDATE public.subscriptions SET ueberlappt_abo_id = NULL
         WHERE id = OLD.neu_abo_id AND ueberlappt_abo_id IS NOT NULL;
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS abo_abschluss_ersatz_aufloesen ON public.abo_abschluesse;
CREATE TRIGGER abo_abschluss_ersatz_aufloesen
    AFTER UPDATE OR DELETE ON public.abo_abschluesse
    FOR EACH ROW EXECUTE FUNCTION public.abo_abschluss_ersatz_aufloesen();

-- (d) Die Kontoinvariante, geprueft zum Commit.
CREATE OR REPLACE FUNCTION public.abo_konto_pruefen(p_user UUID, p_app public.app_id) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    n_regulaer INTEGER;
    n_ersatz   INTEGER;
    r          RECORD;
BEGIN
    PERFORM public.abo_nur_read_committed();
    SELECT count(*) FILTER (WHERE ueberlappt_abo_id IS NULL),
           count(*) FILTER (WHERE ueberlappt_abo_id IS NOT NULL)
      INTO n_regulaer, n_ersatz
      FROM public.subscriptions
     WHERE user_id = p_user AND app_id = p_app
       AND status = 'active' AND mollie_subscription_id IS NOT NULL;
    IF n_regulaer > 1 OR n_ersatz > 1 THEN
        RAISE EXCEPTION 'Abo-Invariante verletzt: Konto %/% hat % regulaere und % Ersatz-Mollie-Abos (erlaubt 1 + hoechstens 1 waehrend eines offenen Wechsels)',
            p_user, p_app, n_regulaer, n_ersatz USING ERRCODE = 'check_violation';
    END IF;

    -- Ein Ersatz existiert nur, solange ein offener Wechsel genau dieses Paar
    -- traegt, und nur neben einem aktiven, regulaeren Mollie-Abo desselben Kontos.
    FOR r IN SELECT id, ueberlappt_abo_id FROM public.subscriptions
              WHERE user_id = p_user AND app_id = p_app AND ueberlappt_abo_id IS NOT NULL LOOP
        IF NOT EXISTS (SELECT 1 FROM public.abo_abschluesse ab
                        WHERE ab.neu_abo_id = r.id AND ab.alt_abo_id = r.ueberlappt_abo_id
                          AND ab.user_id = p_user AND ab.app_id = p_app
                          AND ab.status IN ('neu_aktiv', 'doppelt_aktiv')) THEN
            RAISE EXCEPTION 'Abo-Invariante verletzt: Ersatz % -> % ohne offenen Wechsel (Konto %/%)',
                r.id, r.ueberlappt_abo_id, p_user, p_app USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM public.subscriptions t
                        WHERE t.id = r.ueberlappt_abo_id AND t.user_id = p_user AND t.app_id = p_app
                          AND t.status = 'active' AND t.mollie_subscription_id IS NOT NULL
                          AND t.ueberlappt_abo_id IS NULL) THEN
            RAISE EXCEPTION 'Abo-Invariante verletzt: Ersatz % zeigt auf % — kein aktives, regulaeres Mollie-Abo des Kontos %/%',
                r.id, r.ueberlappt_abo_id, p_user, p_app USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;

    -- Offene Wechsel mit gesetztem neuen Abo: beide Abos gehoeren diesem Konto.
    IF EXISTS (SELECT 1 FROM public.abo_abschluesse ab
                 JOIN public.subscriptions n ON n.id = ab.neu_abo_id
                 JOIN public.subscriptions a ON a.id = ab.alt_abo_id
                WHERE ab.user_id = p_user AND ab.app_id = p_app
                  AND ab.status IN ('neu_aktiv', 'doppelt_aktiv')
                  AND (n.user_id <> p_user OR n.app_id <> p_app
                       OR a.user_id <> p_user OR a.app_id <> p_app)) THEN
        RAISE EXCEPTION 'Abo-Invariante verletzt: offener Wechsel im Konto %/% verweist auf ein fremdes Abo', p_user, p_app
            USING ERRCODE = 'check_violation';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.abo_konto_invariante_trigger() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM public.abo_konto_pruefen(OLD.user_id, OLD.app_id);
    END IF;
    IF TG_OP = 'INSERT' THEN
        PERFORM public.abo_konto_pruefen(NEW.user_id, NEW.app_id);
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS abo_zz_konto_invariante ON public.subscriptions;
CREATE CONSTRAINT TRIGGER abo_zz_konto_invariante
    AFTER INSERT OR DELETE OR UPDATE
    ON public.subscriptions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public.abo_konto_invariante_trigger();

DROP TRIGGER IF EXISTS abo_zz_konto_invariante ON public.abo_abschluesse;
CREATE CONSTRAINT TRIGGER abo_zz_konto_invariante
    AFTER INSERT OR UPDATE OR DELETE ON public.abo_abschluesse
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public.abo_konto_invariante_trigger();

-- (e) TRUNCATE umgeht alle Zeilen-Trigger (Codex R3). Laut abweisen —
-- auch kaskadiert (TRUNCATE users CASCADE erreicht die Tabellen ebenfalls) —
-- und das Recht entziehen: PUBLIC und jedem Inhaber, den Eigentuemer
-- eingeschlossen. Eigentuemer/Superuser koennen beides zuruecknehmen; diese
-- Grenze ist benannt, nicht geschlossen.
CREATE OR REPLACE FUNCTION public.abo_truncate_abweisen() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'TRUNCATE auf % ist gesperrt (Abo-Invariante, Migration 070)', TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END $$;

DROP TRIGGER IF EXISTS abo_truncate_abweisen ON public.subscriptions;
CREATE TRIGGER abo_truncate_abweisen BEFORE TRUNCATE ON public.subscriptions
    FOR EACH STATEMENT EXECUTE FUNCTION public.abo_truncate_abweisen();
DROP TRIGGER IF EXISTS abo_truncate_abweisen ON public.abo_abschluesse;
CREATE TRIGGER abo_truncate_abweisen BEFORE TRUNCATE ON public.abo_abschluesse
    FOR EACH STATEMENT EXECUTE FUNCTION public.abo_truncate_abweisen();

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT DISTINCT c.relname, a.grantee
          FROM pg_class c,
               aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) a
         WHERE c.oid IN ('public.subscriptions'::regclass, 'public.abo_abschluesse'::regclass)
           AND a.privilege_type = 'TRUNCATE'
    LOOP
        IF r.grantee = 0 THEN
            EXECUTE format('REVOKE TRUNCATE ON public.%I FROM PUBLIC', r.relname);
        ELSE
            EXECUTE format('REVOKE TRUNCATE ON public.%I FROM %I', r.relname, pg_get_userbyid(r.grantee));
        END IF;
    END LOOP;
END $$;

-- (f) TEMP-Recht auf der Datenbank entziehen (Codex R4, zweite Schicht neben
-- der festen Namensaufloesung oben). Weder Bridge-Code noch Migrationen noch
-- Betriebsskripte legen temporaere Tabellen an (grep 18.10.2026 ueber src,
-- scripts, bin, docker/migrations). Entzogen wird PUBLIC und jedem
-- ausdruecklichen Inhaber ausser dem Eigentuemer der Datenbank; Superuser
-- bleiben unberuehrt — dort traegt allein die Namensaufloesung.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT a.grantee
          FROM pg_catalog.pg_database d,
               pg_catalog.aclexplode(COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))) a
         WHERE d.datname = pg_catalog.current_database()
           AND a.privilege_type = 'TEMPORARY'
           AND a.grantee <> d.datdba
    LOOP
        IF r.grantee = 0 THEN
            EXECUTE pg_catalog.format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', pg_catalog.current_database());
        ELSE
            EXECUTE pg_catalog.format('REVOKE TEMPORARY ON DATABASE %I FROM %I',
                                      pg_catalog.current_database(), pg_catalog.pg_get_userbyid(r.grantee));
        END IF;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 3. Bestand pruefen, dann die Invariante als Index
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    n INTEGER;
BEGIN
    SELECT count(*) INTO n FROM (
        SELECT 1 FROM public.subscriptions
         WHERE status = 'active' AND mollie_subscription_id IS NOT NULL
           AND ueberlappt_abo_id IS NULL      -- markierter Ersatz ist erlaubt
         GROUP BY user_id, app_id HAVING count(*) > 1
    ) d;
    IF n > 0 THEN
        RAISE EXCEPTION '070: % Konto/Konten mit mehr als einem aktiven Mollie-Abo — erst von Hand klaeren: SELECT user_id, app_id, array_agg(id) FROM public.subscriptions WHERE status=''active'' AND mollie_subscription_id IS NOT NULL AND ueberlappt_abo_id IS NULL GROUP BY 1,2 HAVING count(*)>1', n;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_ein_mollie_abo_je_konto
    ON public.subscriptions (user_id, app_id)
    WHERE status = 'active' AND mollie_subscription_id IS NOT NULL
      AND ueberlappt_abo_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_hoechstens_ein_ersatz_je_konto
    ON public.subscriptions (user_id, app_id)
    WHERE status = 'active' AND mollie_subscription_id IS NOT NULL
      AND ueberlappt_abo_id IS NOT NULL;
