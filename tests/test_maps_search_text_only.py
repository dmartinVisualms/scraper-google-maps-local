"""Tests para el modo text_only de open_maps_and_search."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.scraper import maps_search
from src.scraper.maps_search import open_maps_and_search


class _FakeLocator:
    async def count(self):  # noqa: D401
        return 1

    @property
    def first(self):
        return self

    async def wait_for(self, **_kw):
        return None

    async def fill(self, _value):
        return None

    async def press(self, _key):
        return None


class _FakePage:
    def __init__(self):
        self.goto_urls = []

    async def goto(self, url, **_kw):
        self.goto_urls.append(url)

    def locator(self, _selector):
        return _FakeLocator()


def _patch_helpers(monkeypatch):
    async def _noop_consent(_page):
        return None

    async def _fake_find_input(_page):
        return _FakeLocator()

    monkeypatch.setattr(maps_search, "_maybe_handle_consent", _noop_consent)
    monkeypatch.setattr(maps_search, "_find_search_input", _fake_find_input)
    # asyncio.sleep no necesita patch — sleeps son cortos


def test_text_only_skips_viewport_url(monkeypatch):
    _patch_helpers(monkeypatch)
    page = _FakePage()
    asyncio.run(open_maps_and_search(
        page, "zapaterías en Vedra",
        lat=42.78, lon=-8.45, zoom=16,  # se pasan, pero text_only debe ignorarlos
        text_only=True,
    ))
    assert page.goto_urls == ["https://www.google.com/maps"]
    assert all("@" not in u for u in page.goto_urls)


def test_viewport_mode_uses_at_url(monkeypatch):
    _patch_helpers(monkeypatch)
    page = _FakePage()
    asyncio.run(open_maps_and_search(
        page, "zapaterías", lat=42.78, lon=-8.45, zoom=16,
    ))
    assert len(page.goto_urls) == 1
    assert "@42.78,-8.45,16z" in page.goto_urls[0]


def test_no_coords_falls_back_to_home(monkeypatch):
    _patch_helpers(monkeypatch)
    page = _FakePage()
    asyncio.run(open_maps_and_search(page, "zapaterías"))
    assert page.goto_urls == ["https://www.google.com/maps"]
