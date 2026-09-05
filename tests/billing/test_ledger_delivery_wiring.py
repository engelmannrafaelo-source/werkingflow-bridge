"""Die Auslieferungs-Sonde muss auch wirklich verdrahtet sein.

Ohne die Middleware gibt es keine Sonde — und ohne Sonde bucht jeder
Gateway-Fehler nach dem Modelllauf wieder einen Erfolg (Befund 03.09.2026).
Der Rest der Logik ist in test_ledger_delivery_truth.py geprueft; hier steht
nur die Frage "haengt sie am App".
"""
# Stubs vor jedem src.*-Import — src.main zieht das Claude-Code-SDK und die
# DB-Schicht nach, beides hier weder vorhanden noch noetig. Gleiches Muster
# wie tests/research_cloud/test_anonymize_gate.py.
import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import os  # noqa: E402

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from src.activity.delivery import DeliveryProbeMiddleware  # noqa: E402
import src.main  # noqa: E402


def test_delivery_probe_middleware_is_installed_on_the_app():
    assert any(
        m.cls is DeliveryProbeMiddleware for m in src.main.app.user_middleware
    ), (
        "DeliveryProbeMiddleware ist nicht mehr registriert — die Ledger-Zeile "
        "kann Auslieferung und Modelllauf dann nicht mehr auseinanderhalten"
    )
