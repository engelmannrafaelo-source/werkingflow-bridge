-- 055 — UID-Pruefung gegen VIES wird zum Beleg (Rafael, 2026-08-05)
--
-- _determine_tax_rate (billing_service.py) behandelte bis heute JEDEN
-- nicht-leeren vatId als B2B-Signal: EU-Ausland + irgendein Text im Feld
-- ergab 0 % Umsatzsteuer mit Reverse-Charge-Vermerk. Ein Kunde, der "DE123"
-- oder auch nur einen Tippfehler eingibt, bekam damit eine Nullsteuer-
-- Rechnung.
--
-- Das ist kein kosmetischer Fehler: § 19 Abs 1 UStG verlagert die Steuer-
-- schuld nur bei einer GUELTIGEN UID des Leistungsempfaengers. Ist sie
-- ungueltig, schuldet der Aussteller die Umsatzsteuer — und der Fehler faellt
-- erst bei einer Betriebspruefung auf, dann rueckwirkend fuer alle
-- betroffenen Rechnungen.
--
-- Diese Tabelle haelt das Ergebnis jeder VIES-Abfrage fest. Zweck ist nicht
-- Zwischenspeicherung, sondern AUFZEICHNUNG nach § 18 UStG: eine Pruefung,
-- die niemand belegen kann, ist im Ernstfall keine.
--
-- WARUM DAS ERGEBNIS GESPEICHERT WIRD UND NICHT BEI JEDER RECHNUNG NEU GEFRAGT:
--   • VIES ist ein fremder Dienst mit Ausfaellen. Eine Rechnungserstellung
--     darf nicht daran haengen — und ein Ausfall darf erst recht nicht dazu
--     fuehren, dass "gerade keine Antwort" als "gueltig" durchgeht.
--   • Der Steuerstatus muss zum Zeitpunkt der Leistung belegbar sein, nicht
--     zum Zeitpunkt einer spaeteren Nachfrage.
--
-- WARUM checked_at UND response_raw:
-- Die Bestaetigung altert. Wer spaeter wissen muss, worauf sich eine
-- Nullsteuer-Rechnung stuetzte, findet hier die vollstaendige VIES-Antwort
-- inklusive des von VIES gemeldeten Namens — nicht nur ein Ja/Nein.

CREATE TABLE IF NOT EXISTS vat_id_validations (
    id              UUID PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Normalisiert (ohne Leerzeichen/Punkte, Grossbuchstaben) — so, wie er
    -- abgefragt wurde. Der Rohwert des Kunden steht in tenants.billing_vat_id.
    vat_id          TEXT NOT NULL,
    country_code    CHAR(2) NOT NULL,

    -- Das Ergebnis. NULL gibt es nicht: entweder VIES hat geantwortet
    -- (true/false) oder es wurde kein Datensatz geschrieben.
    is_valid        BOOLEAN NOT NULL,

    -- Von VIES gemeldeter Name/Anschrift, soweit das Mitgliedsland sie
    -- herausgibt (manche liefern nur "---"). Fuer den Abgleich mit der
    -- erfassten Rechnungsadresse.
    vies_name       TEXT,
    vies_address    TEXT,

    -- Vollstaendige Antwort, damit spaeter nachvollziehbar bleibt, worauf
    -- sich die Steuerentscheidung stuetzte.
    response_raw    JSONB NOT NULL,

    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Der Lesezugriff im Steuerpfad fragt immer: "gibt es fuer DIESEN Mandanten
-- und DIESE UID eine bestaetigte Pruefung, und wie alt ist sie?"
CREATE INDEX IF NOT EXISTS vat_id_validations_lookup
    ON vat_id_validations (tenant_id, vat_id, checked_at DESC);

COMMENT ON TABLE vat_id_validations IS
    'VIES-Pruefergebnisse je Mandant/UID. Nur eine bestaetigte Pruefung (is_valid) '
    'erlaubt Reverse Charge; ohne Eintrag gilt der sichere Default (20 % AT USt).';
