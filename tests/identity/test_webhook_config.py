"""
Tests for src.identity.webhook_config — env-driven bootstrap of webhook URLs.
"""
from __future__ import annotations

import pytest

from src.identity.webhook_config import (
    BRIDGE_AUTH_APP_IDS,
    _env_name,
    get_webhook_config,
    init_webhook_configs,
    load_webhook_configs,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Each test gets a clean cache. Clean env vars on entry too — pytest
    inherits the host shell which may have BRIDGE_WEBHOOK_* set."""
    reset_for_tests()
    yield
    reset_for_tests()


def _set_env_for_all_apps(monkeypatch, url_prefix="https://app.example/auth", secret="s3cret"):
    for app in BRIDGE_AUTH_APP_IDS:
        url_var = _env_name(app, "URL")
        secret_var = _env_name(app, "SECRET")
        monkeypatch.setenv(url_var, f"{url_prefix}/{app}")
        monkeypatch.setenv(secret_var, f"{secret}-{app}")


def _clear_env(monkeypatch):
    for app in BRIDGE_AUTH_APP_IDS:
        monkeypatch.delenv(_env_name(app, "URL"), raising=False)
        monkeypatch.delenv(_env_name(app, "SECRET"), raising=False)


class TestEnvNameMapping:
    def test_dashes_to_underscores_and_upper(self):
        assert _env_name("werking-report", "URL") == "BRIDGE_WEBHOOK_URL_WERKING_REPORT"
        assert _env_name("werking-energy", "SECRET") == "BRIDGE_WEBHOOK_SECRET_WERKING_ENERGY"

    def test_single_word_app_id(self):
        assert _env_name("engelmann", "URL") == "BRIDGE_WEBHOOK_URL_ENGELMANN"


class TestLoadWebhookConfigs:
    def test_happy_path_loads_all_apps(self, monkeypatch):
        _set_env_for_all_apps(monkeypatch)
        configs = load_webhook_configs()
        assert set(configs.keys()) == BRIDGE_AUTH_APP_IDS
        for app, cfg in configs.items():
            assert cfg.url.endswith(f"/{app}")
            assert cfg.secret.endswith(f"-{app}")

    def test_missing_secret_fail_loud(self, monkeypatch):
        _set_env_for_all_apps(monkeypatch)
        monkeypatch.delenv(_env_name("werking-energy", "SECRET"), raising=False)
        with pytest.raises(RuntimeError) as exc:
            load_webhook_configs()
        assert "BRIDGE_WEBHOOK_SECRET_WERKING_ENERGY" in str(exc.value)

    def test_missing_url_fail_loud(self, monkeypatch):
        _set_env_for_all_apps(monkeypatch)
        monkeypatch.delenv(_env_name("werking-safety", "URL"), raising=False)
        with pytest.raises(RuntimeError) as exc:
            load_webhook_configs()
        assert "BRIDGE_WEBHOOK_URL_WERKING_SAFETY" in str(exc.value)

    def test_blank_string_counts_as_missing(self, monkeypatch):
        _set_env_for_all_apps(monkeypatch)
        monkeypatch.setenv(_env_name("werking-noise", "SECRET"), "   ")
        with pytest.raises(RuntimeError) as exc:
            load_webhook_configs()
        assert "BRIDGE_WEBHOOK_SECRET_WERKING_NOISE" in str(exc.value)

    def test_missing_lists_all_at_once(self, monkeypatch):
        """Missing two vars: error mentions BOTH (not just the first one
        we hit) — so an operator fixes everything in one restart cycle."""
        _clear_env(monkeypatch)
        with pytest.raises(RuntimeError) as exc:
            load_webhook_configs(frozenset({"werking-report", "werking-energy"}))
        msg = str(exc.value)
        assert "WERKING_REPORT" in msg
        assert "WERKING_ENERGY" in msg


class TestGetWebhookConfig:
    def test_uninitialised_raises(self):
        with pytest.raises(RuntimeError) as exc:
            get_webhook_config("werking-report")
        assert "init_webhook_configs" in str(exc.value)

    def test_initialised_returns_config(self, monkeypatch):
        _set_env_for_all_apps(monkeypatch)
        init_webhook_configs()
        cfg = get_webhook_config("werking-report")
        assert cfg.url.endswith("/werking-report")
        assert cfg.secret.endswith("-werking-report")

    def test_unknown_app_id_raises_lookup_error(self, monkeypatch):
        _set_env_for_all_apps(monkeypatch)
        init_webhook_configs()
        with pytest.raises(LookupError) as exc:
            get_webhook_config("engelmann")  # Supabase, not Bridge-Auth
        assert "engelmann" in str(exc.value)

    def test_reset_clears_cache(self, monkeypatch):
        _set_env_for_all_apps(monkeypatch)
        init_webhook_configs()
        reset_for_tests()
        with pytest.raises(RuntimeError):
            get_webhook_config("werking-report")
