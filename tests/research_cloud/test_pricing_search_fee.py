"""Web-search fee addition to the pricing SSoT (src/pricing.py).

Guards: cost_usd/cost_eur must bill search_count on top of tokens (no
Silent-€0 for server-side web_search usage), while defaulting to 0 so every
existing (non-research-cloud) caller is unaffected.
"""
import pytest

from src.pricing import WEB_SEARCH_FEE_USD, cost_eur, cost_usd, usd_to_eur_rate


def test_search_fee_added_on_top_of_token_cost():
    base = cost_usd("claude-sonnet-5", input_tokens=1000, output_tokens=1000)
    with_searches = cost_usd(
        "claude-sonnet-5", input_tokens=1000, output_tokens=1000, search_count=15
    )
    assert with_searches == pytest.approx(base + 15 * WEB_SEARCH_FEE_USD, abs=1e-9)


def test_zero_searches_is_a_no_op_default():
    without_kw = cost_usd("claude-sonnet-5", input_tokens=1000, output_tokens=1000)
    with_zero = cost_usd(
        "claude-sonnet-5", input_tokens=1000, output_tokens=1000, search_count=0
    )
    assert without_kw == with_zero


def test_search_fee_still_raises_for_unpriced_model():
    with pytest.raises(KeyError):
        cost_usd("no-such-model", input_tokens=1, output_tokens=1, search_count=15)


def test_cost_eur_propagates_search_count():
    usd = cost_usd("claude-sonnet-5", input_tokens=0, output_tokens=0, search_count=15)
    eur = cost_eur("claude-sonnet-5", input_tokens=0, output_tokens=0, search_count=15)
    assert eur == pytest.approx(usd * usd_to_eur_rate(), abs=1e-6)
    assert eur > 0
