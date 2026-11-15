"""Resend-Transport der Bridge: Ziel-URL und Testtransport-Schutz (HOM-STAGE-S3-20261115).

Ohne RESEND_API_BASE_URL ist das Verhalten bitgenau das bisherige: Ziel
https://api.resend.com/emails. Nur wenn die Basis gesetzt ist (Dev/Staging-Testtransport,
z. B. der Resend-Mock), gilt zusaetzlich ein Empfaengergate. In Produktion
(BRIDGE_ORIGIN_ID == "prod") ist jede andere Basis ein Startfehler.

  RESEND_API_BASE_URL              Basis, Default https://api.resend.com
  RESEND_TEST_ALLOWED_RECIPIENTS   komma-getrennte fnmatch-Muster (nur bei gesetzter Basis),
                                   Default "*@example.invalid,*@*.invalid"
  RESEND_REQUIRE_TEST_BASE=true    ohne gesetzte Basis kein Versand (kein stiller Rueckfall
                                   auf echtes Resend), lauter Fehler
"""
from __future__ import annotations

import fnmatch
import logging
import os
from typing import Iterable

DEFAULT_BASE = "https://api.resend.com"
_DEFAULT_ALLOWED = "*@example.invalid,*@*.invalid"


def _base() -> str:
    return os.environ.get("RESEND_API_BASE_URL", "").strip().rstrip("/")


def is_test_transport() -> bool:
    """True, wenn eine von api.resend.com abweichende Basis gesetzt ist."""
    base = _base()
    return bool(base) and base.lower() != DEFAULT_BASE


def resend_emails_url() -> str:
    """Ziel-URL fuer POST /emails. Default exakt https://api.resend.com/emails."""
    base = _base()
    if not base:
        if os.environ.get("RESEND_REQUIRE_TEST_BASE", "").strip().lower() in ("1", "true", "yes", "on"):
            raise RuntimeError(
                "RESEND_REQUIRE_TEST_BASE ist aktiv, aber RESEND_API_BASE_URL ist nicht gesetzt — "
                "kein Versand (kein stiller Rueckfall auf echtes Resend)."
            )
        return DEFAULT_BASE + "/emails"
    return base + "/emails"


def check_recipients(recipients: Iterable[str]) -> None:
    """Bei Testtransport nur erlaubte Empfaenger; sonst lauter Abbruch. Ohne Test-Basis: no-op."""
    if not is_test_transport():
        return
    patterns = [
        p.strip().lower()
        for p in os.environ.get("RESEND_TEST_ALLOWED_RECIPIENTS", _DEFAULT_ALLOWED).split(",")
        if p.strip()
    ]
    for rcpt in recipients:
        addr = (rcpt or "").strip().lower()
        if not any(fnmatch.fnmatchcase(addr, p) for p in patterns):
            raise RuntimeError(
                "Testtransport aktiv: Empfaenger nicht erlaubt (nur Muster aus "
                "RESEND_TEST_ALLOWED_RECIPIENTS)."
            )


def assert_startup_config() -> None:
    """Startfehler: in Produktion nie eine andere Basis als api.resend.com.
    Einmalige Warnung, wenn BRIDGE_ORIGIN_ID nicht dev/staging/prod ist (dann bleibt die
    Auto-Fakturierung ungebunden, siehe invoices.routes.auto_approve_and_send_invoice)."""
    origin = os.environ.get("BRIDGE_ORIGIN_ID", "").strip().lower()
    if origin not in ("dev", "staging", "prod"):
        logging.getLogger(__name__).warning(
            "BRIDGE_ORIGIN_ID=%r ist nicht dev/staging/prod — auto_approve_and_send_invoice bleibt "
            "an INVOICE_REQUIRE_APPROVAL ungebunden (bisheriges Verhalten).", origin,
        )
    if is_test_transport() and origin == "prod":
        raise RuntimeError(
            "RESEND_API_BASE_URL ist in Produktion (BRIDGE_ORIGIN_ID=prod) nicht zulaessig."
        )
