import urllib.error
from unittest.mock import Mock

import pytest

from src.erkunder.gesundheit import probe


@pytest.mark.parametrize("role", ["leitstand", "platz"])
@pytest.mark.parametrize("code", [200, 403, 404, 500])
def test_http_readiness_requires_authenticated_missing_route(monkeypatch, role, code):
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "synthetic-token")
    opener = Mock()
    if code != 200:
        opener.open.side_effect = urllib.error.HTTPError("", code, "", {}, None)
    monkeypatch.setattr("urllib.request.build_opener", Mock(return_value=opener))
    if code == 404:
        probe(role)
    else:
        with pytest.raises(RuntimeError):
            probe(role)
    request = opener.open.call_args.args[0]
    assert request.get_header("X-erkunder-intern") == "synthetic-token"


def test_http_readiness_connection_failure_is_loud(monkeypatch):
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "synthetic-token")
    monkeypatch.setattr(
        "urllib.request.build_opener",
        Mock(return_value=Mock(open=Mock(side_effect=ConnectionRefusedError))),
    )
    with pytest.raises(ConnectionRefusedError):
        probe("leitstand")
