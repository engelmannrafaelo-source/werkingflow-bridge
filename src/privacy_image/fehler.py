"""Fehler der Bild-Schnittstelle: Status + Code + fester Text. Nie Bytes, OCR-Text, Dateinamen oder Koordinaten."""
from __future__ import annotations

API_VERSION = "bild-api-v1"

_TEXTE = {
    "image_mode_invalid": "modus fehlt oder ist unbekannt",
    "image_field_unknown": "unbekanntes Feld",
    "image_field_duplicate": "Feld doppelt",
    "image_handmask_invalid": "Handmaske ungueltig",
    "image_handmask_too_large": "Handmaskenfeld zu gross",
    "image_handmask_not_allowed": "Feld bei diesem modus nicht erlaubt",
    "image_mask_out_of_bounds": "Maske ausserhalb des Bildes",
    "image_mask_space_mismatch": "Koordinatenraum passt nicht zum Bild",
    "image_preview_mismatch": "final-Bytes passen nicht zur Vorschau",
    "image_requires_idempotency_key": "Bild-Job braucht Idempotency-Key",
    "image_count_exceeded": "hoechstens ein Bild je Job",
    "image_receipt_binding_invalid": "Bild und image_receipt_id gehoeren zusammen",
    "image_mask_principal_not_allowed": "Principal fuer Bildmaske nicht zugelassen",
    "image_requires_job": "Bild nur im gebundenen Job",
    "image_final_token_invalid": "final_token ungueltig",
    "image_final_token_expired": "final_token abgelaufen",
    "image_receipt_missing": "Receipt fehlt",
    "image_receipt_invalid": "Receipt ungueltig",
    "image_receipt_expired": "Receipt abgelaufen",
    "image_receipt_principal_mismatch": "Receipt gehoert anderem Principal/Tenant",
    "image_receipt_sha_mismatch": "Bildbytes passen nicht zum Receipt",
    "image_receipt_confirmation_missing": "Bestaetigung fehlt oder unplausibel",
    "image_empty_requires_second_confirmation": "keine Schrift erkannt: Zweitbestaetigung noetig",
    "image_route_not_allowed": "Bildweg fuer diese Route nicht freigegeben",
    "feature_disabled": "Funktion nicht aktiviert",
    "image_receipt_consumed": "Receipt schon verbraucht",
    "image_receipt_conflict": "Receipt-Konflikt",
    "image_too_large": "Bild zu gross",
    "image_type_unsupported": "nur PNG oder JPEG",
    "image_unreadable": "Bild nicht lesbar",
    "image_reencode_failed": "Neukodierung fehlgeschlagen",
    "image_mask_unavailable": "Bildmaske derzeit nicht verfuegbar",
}


class BildFehler(Exception):
    """Fachlicher Fehler mit festem Text. ``str()`` enthaelt nie Eingabedaten."""

    def __init__(self, status: int, code: str):
        if code not in _TEXTE:
            raise ValueError("unbekannter Bild-Fehlercode")
        super().__init__(code)
        self.status = status
        self.code = code

    def body(self) -> dict:
        typ = "invalid_request_error" if self.status < 500 else "api_error"
        if self.status in (401, 403):
            typ = "permission_error"
        return {"error": {"type": typ, "code": self.code, "message": _TEXTE[self.code]}}
