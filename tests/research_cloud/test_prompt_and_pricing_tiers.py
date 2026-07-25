from src.research_cloud.pricing_tiers import customer_price_eur
from src.research_cloud.prompt import build_system_prompt, search_budget_for_depth


def test_system_prompt_mentions_depth_instruction():
    prompt = build_system_prompt("deep")
    assert "gründlich" in prompt
    assert "Quellenliste" in prompt


def test_system_prompt_falls_back_for_unknown_depth():
    prompt = build_system_prompt("not-a-real-depth")
    assert prompt == build_system_prompt("standard")


def test_search_budget_scales_with_depth():
    quick = search_budget_for_depth("quick")
    standard = search_budget_for_depth("standard")
    deep = search_budget_for_depth("deep")
    exhaustive = search_budget_for_depth("exhaustive")
    assert quick[0] < standard[0] < deep[0] < exhaustive[0]
    assert quick[1] < standard[1] < deep[1] < exhaustive[1]


def test_search_budget_falls_back_for_unknown_depth():
    assert search_budget_for_depth(None) == search_budget_for_depth("standard")


def test_customer_price_defaults_and_env_override(monkeypatch):
    assert customer_price_eur("quick") == 1.0
    assert customer_price_eur("standard") == 3.0
    assert customer_price_eur("deep") == 8.0
    assert customer_price_eur(None) == 3.0  # unknown -> standard default

    monkeypatch.setenv("RESEARCH_CLOUD_PRICE_DEEP_EUR", "9.5")
    assert customer_price_eur("deep") == 9.5
