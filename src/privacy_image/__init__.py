"""Bild-PII-Maske (Bildvertrag v3/v4, Schnittstelle Bild v2 "bild-api-v1").

Reine Bausteine ohne Netz und ohne Dateien:
  fehler        -- BildFehler(status, code) mit festen Texten (nie Inhalte)
  formular      -- strenge Pruefung der Multipart-/JSON-Felder
  verarbeitung  -- feste Reihenfolge Frame 0 -> Orientierung -> Modellmasse -> Alpha -> OCR/Schwaerzen
                   -> Neukodierung ohne Metadaten -> Scan -> sha256 (nur im Speicher)
  token         -- final_token (HMAC) ausstellen/pruefen

Bridge-Endpunkte, Receipt-Speicher und Executor-Pruefung binden diese Bausteine ein.
"""
