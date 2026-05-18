"""
Break the circular import chain that arises in test when billing modules
import src.api_auth which in turn touches src.identity.routes.
Also stub asyncpg when it is not installed (invoices/routes.py imports it
at module level unlike db/client.py which has a try/except guard).
Same approach as tests/budget/conftest.py.
"""
import sys
from unittest.mock import MagicMock

if "src.identity.routes" not in sys.modules:
    _stub = MagicMock()
    _stub.router = MagicMock()
    sys.modules["src.identity.routes"] = _stub

try:
    import asyncpg  # noqa: F401
except ImportError:
    _asyncpg_stub = MagicMock()
    _asyncpg_stub.UniqueViolationError = type("UniqueViolationError", (Exception,), {})
    _asyncpg_stub.Connection = MagicMock
    sys.modules["asyncpg"] = _asyncpg_stub
