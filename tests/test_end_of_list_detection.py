"""Tests para `_has_reached_end` — detector robusto del mensaje de fin de lista."""
from __future__ import annotations

import asyncio

import pytest

from src.scraper.maps_search import _has_reached_end


class _FakeLocator:
    """Simula el resultado de page.locator(): cuenta = 1 si la query coincide
    (modeladamente) con cualquier substring del DOM simulado."""

    def __init__(self, matched: bool):
        self._matched = matched

    async def count(self) -> int:
        return 1 if self._matched else 0


class _FakePage:
    def __init__(self, dom_text: str):
        self.dom_text = dom_text

    def locator(self, selector: str):
        # Simula el comportamiento de :text-matches("texto", "i").
        # Extrae la cadena entre las primeras comillas tras `text-matches(`.
        marker = 'text-matches("'
        if marker in selector:
            start = selector.index(marker) + len(marker)
            end = selector.index('"', start)
            needle = selector[start:end].lower()
            return _FakeLocator(needle in self.dom_text.lower())
        # Fallback: devolver no-match
        return _FakeLocator(False)


def _check(dom_text: str) -> bool:
    page = _FakePage(dom_text)
    return asyncio.run(_has_reached_end(page))


@pytest.mark.parametrize("dom,expected", [
    # Caso real reproducido por el usuario (con punto final)
    ("Has llegado al final de la lista.", True),
    # Sin punto
    ("Has llegado al final de la lista", True),
    # Sólo el fragmento principal
    ("llegado al final de la lista", True),
    # Inglés con apóstrofo
    ("You've reached the end of the list", True),
    ("You've reached the end of the list.", True),
    # Variante alternativa
    ("You have reached the end of the list", True),
    # Mayúsculas/minúsculas
    ("HAS LLEGADO AL FINAL DE LA LISTA.", True),
    # No debe disparar con otros mensajes
    ("Mostrando resultados", False),
    ("No se encontraron resultados", False),
    ("", False),
])
def test_has_reached_end_variants(dom, expected):
    assert _check(dom) is expected
