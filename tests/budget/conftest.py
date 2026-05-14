"""
Break the circular import chain that exists in production (and only surfaces in test):
  src.budget.__init__ → routes → api_auth → api_auth.deps → identity.jwt_utils
  → identity.__init__ → identity.routes → api_auth  (partial, circular)

Stubbing out identity.routes before it is imported prevents identity.__init__ from
triggering the re-import of a partially-initialized api_auth.
"""
import sys
from unittest.mock import MagicMock

if "src.identity.routes" not in sys.modules:
    _stub = MagicMock()
    _stub.router = MagicMock()
    sys.modules["src.identity.routes"] = _stub
