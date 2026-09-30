-- 062 — Herkunft der Token-Anforderung an der Webhook-Zustellung (Haus Dev, 2026-09-30)
--
-- Rafael, 30.09.2026 (e-reset-link-ziel-20260930, Antwort a): ein Kunde setzt
-- sein vergessenes Passwort dort neu, wo er es angefordert hat.
--
-- werking-tools teilt das Bridge-Konto, hat aber keinen eigenen Webhook-
-- Empfaenger; seine Reset-Mail geht ueber den Empfaenger von werking-report
-- (_MAIL_DELEGATE_APP_IDS in src/identity/routes.py). `app_id` bleibt der
-- EMPFAENGER, diese Spalte haelt die X-App-ID des ausloesenden Aufrufs. Der
-- Dispatcher schickt sie als `appId` in der Nutzlast mit; der Empfaenger baut
-- daraus den Link.
--
-- TEXT statt app_id-Enum: werking-tools steht nicht im Enum, und die Spalte
-- ist reine Weitergabe, kein Fremdschluessel. NULL bei Altzeilen — dann
-- schickt der Dispatcher kein `appId` (Verhalten wie vorher).
--
-- Rein additiv, nullable, ohne Default: kein Tabellen-Rewrite, alter Code
-- laeuft gegen das neue Schema unveraendert weiter.

ALTER TABLE auth_token_webhook_deliveries
    ADD COLUMN IF NOT EXISTS origin_app_id TEXT;
