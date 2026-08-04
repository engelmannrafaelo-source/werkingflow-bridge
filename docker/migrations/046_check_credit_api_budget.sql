-- 046 — Check-Credits bekommen ein eigenes KI-Budget je Akt (Rafael, 2026-08-04)
--
-- Korrigiert die Annahme aus 041/043 ("api_budget_eur=0: ein Credit schaltet
-- einen Download frei, kein KI-Budget"). Die galt für ein Produkt, bei dem der
-- Credit nur eine fertige Datei aufsperrt. Inzwischen hängt echte KI-Arbeit
-- hinter dem Credit: die korrigierte Fassung (check-korrektur-patch) wird erst
-- NACH dem Freischalten erzeugt. Mit api_budget_eur=0 lief sie deshalb auf den
-- Monatstopf des Nutzers — und wurde dort von der vorangegangenen Gratis-
-- Prüfung aufgebraucht.
--
-- Live beobachtet am 03.08. (Nutzer 5706a112): Gratis-Check 4,455 EUR +
-- Vorarbeit 0,25 EUR = 4,709 EUR auf einem 5-EUR-Trial-Topf. Der um 11:11
-- angeforderte Korrektur-Patch bekam 0 Token — der Kunde hatte um 10:22
-- bezahlt.
--
-- Warum 5,00 und nicht knapper: der Wert deckt nur den Teil NACH dem
-- Freischalten. Gemessene Vollkosten einer korrigierten Fassung: 0,12–0,33 EUR
-- (usage_events, agent_id='check-korrektur-patch', 5 Läufe 02.–03.08.). 5 EUR
-- sind also ~15× Kopffreiheit. Das ist Absicht: die Zahl ist eine Reißleine
-- gegen Amoklauf, KEINE Kundenschranke — sie darf einen zahlenden Kunden nie
-- treffen. Kosten sind ohnehin notional (billing_mode='flat_rate_estimated',
-- real_cost_eur=0,00; abgerechnet wird eine Pauschale), ein höheres Dach kostet
-- also kein echtes Geld.
--
-- Die Gratis-Prüfung VOR der Zahlung ist hiervon nicht berührt — die läuft
-- weiter auf Trial-/Sammelkonto und braucht eine eigene Notbremse.

UPDATE plans
   SET api_budget_eur = 5.00
 WHERE id IN ('report-check-credit', 'report-check-credit-5', 'report-check-credit-1');
