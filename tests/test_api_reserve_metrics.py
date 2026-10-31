from unittest.mock import patch
from fastapi.testclient import TestClient
from src.metrics_reader import main as reader


def test_api_reserve_requires_operator_key_before_reading_balance(monkeypatch):
    monkeypatch.setenv("AI_BRIDGE_API_KEY", "operator-test")
    monkeypatch.delenv("AI_BRIDGE_API_KEY_PREVIOUS", raising=False)
    with patch.object(reader, "api_reserve_snapshot", return_value={"available_usd":790}) as snapshot:
        client = TestClient(reader.app)
        assert client.get("/v1/metrics/api-reserve").status_code == 401
        assert client.get("/v1/metrics/api-reserve", headers={"Authorization":"Bearer wrong"}).status_code == 401
        snapshot.assert_not_called()
        response = client.get("/v1/metrics/api-reserve", headers={"Authorization":"Bearer operator-test"})
        assert response.status_code == 200
        assert response.json()["available_usd"] == 790


def test_keyless_routing_status_never_includes_money(monkeypatch):
    monkeypatch.setattr(reader, 'BRIDGE_WORKERS', [])
    monkeypatch.setattr(reader, '_read_penalties', lambda: {})
    monkeypatch.setattr(reader, 'api_reserve_snapshot', lambda: {
        'state':'available', 'active_on_this_bridge':True, 'available_usd':790,
        'remaining_usd':798, 'granted_usd':800})
    data = TestClient(reader.app).get('/v1/metrics/account-pool-state').json()['api_reserve']
    assert data == {'state':'available', 'active_on_this_bridge':True,
                    'automatic_paid_fallback':False, 'available':True}
