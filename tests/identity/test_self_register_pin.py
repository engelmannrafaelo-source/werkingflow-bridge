"""Der Bedrock-Geburts-Pin ist eine pro-Bridge-Zusage, kein Code-Default.

Rafael 2026-08-24: Pin nur auf der Prod-Bridge (Flag im Prod-Compose). Auf
Dev/Staging hatte die Geburts-Regel Test-/Probe-/Durchlauf-Accounts auf Bedrock
gepinnt und echtes AWS-Geld fuer Testlaeufe verbrannt.
"""

import sys
from unittest.mock import MagicMock

import pytest

# asyncpg is a C extension not present in the unit-test image — same stub
# pattern as tests/identity/test_admin_users_lookup.py.
try:
    import asyncpg  # noqa: F401
except ModuleNotFoundError:
    _asyncpg_stub = MagicMock()
    _asyncpg_stub.UniqueViolationError = type("UniqueViolationError", (Exception,), {})
    _asyncpg_stub.ForeignKeyViolationError = type("ForeignKeyViolationError", (Exception,), {})
    _asyncpg_stub.PostgresError = type("PostgresError", (Exception,), {})
    sys.modules["asyncpg"] = _asyncpg_stub

from src.identity.routes import _SELF_REGISTER_PROVIDER_PIN, _self_register_provider_pin  # noqa: E402


def test_pin_off_by_default(monkeypatch):
    """Ohne expliziten Schalter (Dev/Staging) wird NICHT gepinnt."""
    monkeypatch.delenv("SELF_REGISTER_BEDROCK_PIN_ENABLED", raising=False)
    assert _self_register_provider_pin() is None


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE", " True "])
def test_pin_on_when_enabled(monkeypatch, value):
    """Mit Schalter (Prod-Compose) kommt exakt die zugesagte EU-Pin-Shape."""
    monkeypatch.setenv("SELF_REGISTER_BEDROCK_PIN_ENABLED", value)
    assert _self_register_provider_pin() == _SELF_REGISTER_PROVIDER_PIN
    assert _SELF_REGISTER_PROVIDER_PIN == {"provider": "bedrock", "region": "eu-central-1"}


@pytest.mark.parametrize("value", ["false", "0", "", "off", "enabled-ish"])
def test_pin_off_on_non_truthy_values(monkeypatch, value):
    """Nur die kanonischen Wahr-Werte schalten die Zusage scharf — Tippfehler pinnen nicht."""
    monkeypatch.setenv("SELF_REGISTER_BEDROCK_PIN_ENABLED", value)
    assert _self_register_provider_pin() is None
