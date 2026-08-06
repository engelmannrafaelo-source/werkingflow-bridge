-- 056 — Rücktrittsverzicht bei sofortiger Bereitstellung (Rafael, 2026-08-06)
--
-- WOZU, WENN DER VERKAUF B2B-ONLY IST
-- Ist. Und soll es bleiben — die AGB (Fassung 3.0) schliessen Verbraucher aus,
-- und der Kaeufer erklaert seine Unternehmereigenschaft gesondert. Diese Spalte
-- ist die RUECKFALLEBENE fuer den Fall, dass doch einmal ein Verbraucher
-- durchrutscht: die Verbrauchereigenschaft beurteilt sich nach den Umstaenden,
-- nicht nach der Selbstauskunft, und eine Klausel allein traegt sie nicht.
--
-- Tritt der Fall ein, greift § 18 Abs 1 Z 11 FAGG: das 14-taegige
-- Ruecktrittsrecht bei digitalen Leistungen erlischt nur, wenn der Verbraucher
-- (a) AUSDRUECKLICH verlangt hat, dass vor Ablauf der Frist mit der
-- Bereitstellung begonnen wird, und (b) zur Kenntnis genommen hat, dass er
-- damit sein Ruecktrittsrecht verliert. Ohne diesen Beleg bleibt das Recht
-- bestehen — und der Kunde kann nach vollstaendiger Nutzung des Guthabens
-- zurueckreten.
--
-- Grenzkosten praktisch null, Deckung fuer den einen Fall, der sonst teuer
-- wird. Die B2B-Positionierung wird davon nicht beruehrt: die Erklaerung
-- spricht bewusst von einem "allfaelligen" Ruecktrittsrecht.
--
-- NULLABLE, nicht NOT NULL:
-- Zustimmungen aus der Zeit vor dieser Migration haben die Erklaerung nicht
-- eingeholt. Sie nachtraeglich mit einem Default auf true zu setzen waere eine
-- Faelschung — der Kunde hat sie nie abgegeben. NULL heisst hier "wurde nicht
-- gefragt", und genau das soll spaeter erkennbar bleiben.

ALTER TABLE purchase_consents
    ADD COLUMN IF NOT EXISTS immediate_start_requested boolean;

COMMENT ON COLUMN purchase_consents.immediate_start_requested IS
    'Ausdrueckliches Verlangen nach sofortigem Leistungsbeginn samt Kenntnisnahme '
    'des Verlusts eines allfaelligen Ruecktrittsrechts (§ 18 Abs 1 Z 11 FAGG). '
    'NULL = vor Migration 056 erhoben, Erklaerung wurde nicht eingeholt.';
