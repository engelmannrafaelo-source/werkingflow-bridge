"""Metrics protection must be probed as an external request, even from dev."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location("smoke_external_metrics", Path(__file__).parents[2] / "scripts/bridge_smoke.py")
smoke = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smoke
spec.loader.exec_module(smoke)

@pytest.mark.parametrize("probe", [smoke._metrics_anonymization, smoke._metrics_document_performance])
@pytest.mark.parametrize("status,upstream,expected", [(403,False,True),(200,False,False),(401,False,False),(403,True,False)])
def test_external_metrics_protection(monkeypatch, probe, status, upstream, expected):
    def get(url, *, headers, timeout, allow_redirects):
        assert headers["X-Forwarded-For"] == "198.51.100.1"
        assert headers["Authorization"] == "Bearer test"
        assert allow_redirects is False
        response_headers = {"Server":"openresty/1.27.1.1", "Content-Type":"text/html"}
        if upstream:
            response_headers["X-Bridge-Served-By"] = "dev/worker1"
        return SimpleNamespace(status_code=status, headers=response_headers,
            text="<title>403 Forbidden</title><h1>403 Forbidden</h1><center>openresty/1.27.1.1</center>")
    monkeypatch.setattr(smoke.requests, "get", get)
    assert probe(smoke.Ctx("http://bridge.invalid", "test")).ok is expected

def test_other_routes_keep_normal_internal_headers():
    assert "X-Forwarded-For" not in smoke._lb_emergency_headers(smoke.Ctx("http://bridge.invalid","test"), "/health")
