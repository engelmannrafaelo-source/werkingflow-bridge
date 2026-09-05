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


# ---------------------------------------------------------------------------
# Library catalogue in the system prompt (2026-09-05). The A/B run of
# 2026-09-04 measured 0 library calls in 4 of 4 questions while the tools were
# armed and offered; the catalogue exists so the model no longer has to call a
# tool to find out whether calling it is worthwhile.
# ---------------------------------------------------------------------------

_INDEX = {
    "documents": [
        {
            "id": "at-tirol-tbv2026-anlage1",
            "title": "OIB-Richtlinie 1 — Mechanische Festigkeit",
            "jurisdiction": "AT (harmonisiert)",
            "publisher": "OIB",
            "note": "Volltext der Richtlinie.",
        },
        {
            "id": "ext-klimaaktiv-publikationen",
            "title": "klimaaktiv-Publikationen (BMK)",
            "jurisdiction": "AT",
            "publisher": "BMK",
            "note": "KATALOG-EINTRAG OHNE VOLLTEXT",
        },
    ]
}


def test_prompt_no_longer_prescribes_the_web_search_route():
    """The one sentence that named a method named the web search — while the
    library sat unused. It must not preselect a source any more."""
    assert "per Websuche" not in build_system_prompt("standard", library_index=_INDEX)
    assert "per Websuche" not in build_system_prompt("standard")


def test_prompt_without_library_carries_no_catalogue():
    prompt = build_system_prompt("standard")
    assert "Kuratierte Dokumentbibliothek" not in prompt
    assert "library_get" not in prompt


def test_prompt_lists_every_entry_with_id_and_jurisdiction():
    prompt = build_system_prompt("standard", library_index=_INDEX)
    assert "Verzeichnis (2 Einträge)" in prompt
    assert "`at-tirol-tbv2026-anlage1`" in prompt
    assert "OIB-Richtlinie 1 — Mechanische Festigkeit" in prompt
    assert "[AT (harmonisiert)]" in prompt


def test_catalogue_only_entries_are_marked_so_library_get_is_not_wasted_on_them():
    prompt = build_system_prompt("standard", library_index=_INDEX)
    line = next(l for l in prompt.splitlines() if "ext-klimaaktiv" in l)
    assert "KEIN VOLLTEXT" in line
    fulltext_line = next(l for l in prompt.splitlines() if "at-tirol-tbv2026" in l)
    assert "KEIN VOLLTEXT" not in fulltext_line


def test_catalogue_states_the_source_ranking():
    prompt = build_system_prompt("deep", library_index=_INDEX)
    assert "maßgebliche Quelle" in prompt
    assert "library_get" in prompt


def test_empty_index_renders_no_catalogue():
    assert "Kuratierte Dokumentbibliothek" not in build_system_prompt(
        "standard", library_index={"documents": []}
    )
