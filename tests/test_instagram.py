import asyncio

from src.analyzer import instagram as ig


def test_handle_from_title_with_wrapped_urls():
    items = [
        {"type": "organic", "title": "Vestido de @uniqueunlimited_spain", "url": "https://google.com/goto?url=a"},
        {"type": "organic", "title": "Thebes Moda (@thebesmoda) - Instagram", "url": "https://google.com/goto?url=b"},
    ]
    assert ig.handle_from_serp_items(items, "Thebes") == "thebesmoda"


def test_rejects_profile_of_another_business():
    items = [{"type": "organic", "title": "Zara (@zara) - Instagram"}]
    assert ig.handle_from_serp_items(items, "Calzados Raquel") is None


def test_budget_stops_at_limit_and_counts_pending():
    b = ig.DataForSeoBudget(max_usd=0.01)
    assert b.can_spend()
    b.charge(0.01)
    assert not b.can_spend() and not b.can_spend()
    assert b.pending == 2 and b.estimated_extra_usd >= 0.02


def test_cache_avoids_paid_search(tmp_path, monkeypatch):
    cache = ig.SearchCache(tmp_path / "c.json")
    cache.put("Thebes", "A Estrada", "https://instagram.com/thebesmoda")

    async def boom(*a, **k):
        raise AssertionError("no debería llamar a DataForSEO")

    monkeypatch.setattr(ig, "search_instagram_via_dataforseo", boom)
    budget = ig.DataForSeoBudget(1.0)
    res = asyncio.run(ig.resolve_instagram(None, "Thebes", "A Estrada", ("u", "p"), budget, cache))
    assert res == ("https://instagram.com/thebesmoda", "dataforseo")
    assert budget.spent == 0


def test_picks_best_candidate_over_first():
    items = [
        {"type": "organic", "title": "Dandara | Moda para Mujer (@dandara_ontinyent)"},
        {"type": "organic", "title": "DANDARA CARMONA (@dandara_carmona)"},
        {"type": "organic", "title": "D A N D A R A (@dandara_spain)"},
    ]
    assert ig.handle_from_serp_items(items, "Dándara", "A Estrada") == "dandara_spain"


def test_rejects_truncated_handle():
    assert ig.handle_from_serp_items([{"type": "organic", "title": "Mi Vestidor Azul (@mi...)"}], "Mi Vestidor Azul") is None


def test_title_without_instagram_suffix_and_other_networks():
    assert ig.handle_from_serp_items([{"type": "organic", "title": "Thebes Moda (@thebesmoda)"}], "Thebes") == "thebesmoda"
    assert ig.handle_from_serp_items([{"type": "organic", "title": "Thebes (@thebes) | TikTok"}], "Thebes") is None
