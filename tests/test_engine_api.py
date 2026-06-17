from __future__ import annotations

import asyncio

import pytest

from src.engine_api import usage
from src.engine_api.client import SearchTextResult, search_text
from src.engine_api.cost import estimate_cost, free_cap
from src.engine_api.engine import _dedup
from src.engine_api.errors import FatalHTTPError
from src.engine_api.mapper import map_place


# ── cost ─────────────────────────────────────────────────────────────────
def test_enterprise_free_cap_is_1000_not_10000():
    assert free_cap("enterprise") == 1000
    assert free_cap("pro") == 5000


def test_cost_within_and_above_free():
    assert estimate_cost(1000, tier="enterprise").est_cost_usd == 0.0
    assert estimate_cost(2000, tier="enterprise").est_cost_usd == 35.00
    assert estimate_cost(6000, tier="pro").est_cost_usd == 32.00


# ── mapper ───────────────────────────────────────────────────────────────
def test_map_place_reads_localized_and_falls_back():
    place = {
        "displayName": {"text": "Calzados Mara"},
        "formattedAddress": "Rúa X, Ferrol",
        "internationalPhoneNumber": "+34 981 00 00 00",
        "rating": 4.0,
        "googleMapsUri": "https://maps.google.com/?cid=7",
    }
    rec = map_place(place, source_query="q", municipio_origen="Ferrol", retrieved_at_utc="t")
    assert rec.nombre == "Calzados Mara"
    assert rec.telefono == "+34 981 00 00 00"   # fallback a international
    assert rec.rating == "4.0"
    assert rec.categoria == ""                    # primaryTypeDisplayName ausente
    assert rec.maps_url == "https://maps.google.com/?cid=7"


def test_map_place_sets_lat_lon_from_location():
    place = {"displayName": {"text": "X"}, "location": {"latitude": 43.56, "longitude": -7.25}}
    rec = map_place(place, source_query="q", municipio_origen="Foz", retrieved_at_utc="t")
    assert rec.lat == "43.56" and rec.lon == "-7.25"
    rec2 = map_place({"displayName": {"text": "Y"}}, source_query="q", municipio_origen="Foz", retrieved_at_utc="t")
    assert rec2.lat == "" and rec2.lon == ""


# ── dedup (regresión del bug "?cid=" colapsado) ─────────────────────────────
def test_dedup_keeps_distinct_cid_urls():
    from src.domain import BusinessRecord

    def rec(cid):
        return BusinessRecord(
            nombre="X", telefono="", direccion="", web="", rating="",
            categoria="", source_query="", retrieved_at_utc="",
            maps_url=f"https://maps.google.com/?cid={cid}&g_mp=Z",
        )

    assert len(_dedup([rec(1), rec(2), rec(1)])) == 2


# ── client (sesión simulada, sin red) ───────────────────────────────────────
class _Resp:
    def __init__(self, status, payload):
        self._s, self._p = status, payload

    @property
    def status(self):
        return self._s

    async def json(self):
        return self._p


class _Session:
    def __init__(self, responses):
        self._r = list(responses)
        self.calls = 0

    def post(self, url, headers=None, json=None):
        self.calls += 1
        r = self._r.pop(0)

        async def _c():
            return r

        return _c()


def _run(session, **kw):
    return asyncio.run(search_text("q", "KEY", session=session, retry_base_delay=0.0, **kw))


def test_client_paginates_and_retries_and_fatal():
    s = _Session([
        _Resp(200, {"places": [{"x": 1}], "nextPageToken": "t"}),
        _Resp(200, {"places": [{"x": 2}]}),
    ])
    r = _run(s)
    assert isinstance(r, SearchTextResult) and r.pages == 2 and len(r.places) == 2

    s2 = _Session([_Resp(429, {}), _Resp(200, {"places": [{"x": 1}]})])
    assert len(_run(s2).places) == 1 and s2.calls == 2  # reintenta 429

    s3 = _Session([_Resp(400, {"error": "bad"})])
    with pytest.raises(FatalHTTPError):
        _run(s3)
    assert s3.calls == 1  # 4xx no reintenta


def test_client_strict_type_filter_in_body():
    captured = {}

    class _Cap(_Session):
        def post(self, url, headers=None, json=None):
            captured.update(json)
            return super().post(url, headers=headers, json=json)

    _run(_Cap([_Resp(200, {"places": []})]), included_type="shoe_store")
    assert captured["includedType"] == "shoe_store"
    assert captured["strictTypeFiltering"] is True


# ── usage (contador mensual) ────────────────────────────────────────────────
def test_usage_accumulates_and_flips_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "_USAGE_DIR", tmp_path)
    a = usage.record_and_report(600)
    b = usage.record_and_report(600)  # 1200 > 1000
    assert a["within_free"] is True and a["month_requests"] == 600
    assert b["within_free"] is False and b["remaining"] == 0
    assert b["est_cost_usd"] == round((200 / 1000) * 35.0, 4)


# ── server arma el comando con los flags del motor API ──────────────────────
def test_server_cmd_includes_api_flags():
    import server
    cmd = server._build_scraper_cmd(
        {"category": "zapaterías", "city": "Ferrol", "engine": "api",
         "included_type": "shoe_store"},
        "out/x.csv", resume_csv=None,
    )
    assert "--engine" in cmd and "api" in cmd
    assert "--included-type" in cmd and "shoe_store" in cmd


def test_guess_type_from_category():
    from src.engine_api.category_types import guess_type
    assert guess_type("zapaterías") == "shoe_store"
    assert guess_type("Tiendas de ropa") == "clothing_store"
    assert guess_type("Tiendas de deportes") == "sporting_goods_store"
    assert guess_type("Tiendas de niños") == "clothing_store"
    assert guess_type("restaurantes") == "restaurant"
    assert guess_type("cosa rara sin tipo") is None


def test_resolve_api_key_env_wins_then_local_file(tmp_path, monkeypatch):
    from src.engine_api import secrets

    f = tmp_path / ".env"
    f.write_text('GOOGLE_MAPS_API_KEY="fromfile"\n', encoding="utf-8")
    monkeypatch.setattr(secrets, "_ENV_FILE", f)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    assert secrets.resolve_api_key() == "fromfile"          # fallback al .env
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "fromenv")
    assert secrets.resolve_api_key() == "fromenv"           # el entorno gana


def test_server_cmd_scraper_has_no_api_flags():
    import server
    cmd = server._build_scraper_cmd(
        {"category": "bar", "city": "Lugo", "engine": "scraper"},
        "out/y.csv", resume_csv=None,
    )
    assert "--engine" not in cmd
