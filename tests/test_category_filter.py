"""Tests para src.pipeline.category_filter."""
from __future__ import annotations

import pytest

from src.pipeline.category_filter import category_matches, _normalize, _stem


# ── helpers ──────────────────────────────────────────────────────────────────

def test_normalize_removes_accents():
    assert _normalize("Zapatería") == "zapateria"
    assert _normalize("ZAPATERÍAS") == "zapaterias"


def test_stem_removes_trailing_s():
    assert _stem("zapaterias") == "zapateria"
    assert _stem("Zapatería") == "zapateria"
    assert _stem("zapaterías") == "zapateria"


# ── categoria vacía → siempre acepta ─────────────────────────────────────────

def test_empty_category_accepted():
    assert category_matches("zapateria", "") is True
    assert category_matches("zapateria", "   ") is True


# ── variantes de "zapatería" ─────────────────────────────────────────────────

@pytest.mark.parametrize("categoria", [
    "Zapatería",
    "zapateria",
    "Zapaterías",
    "zapaterias",
    "ZAPATERÍA",
    "Zapatería y artículos de cuero",
])
def test_zapateria_variants_accepted(categoria):
    assert category_matches("zapateria", categoria) is True


@pytest.mark.parametrize("search_term", [
    "zapateria",
    "zapatería",
    "zapaterias",
    "zapaterías",
])
def test_search_term_variants_accepted(search_term):
    assert category_matches(search_term, "Zapatería") is True


# ── categorías no relacionadas → rechaza ─────────────────────────────────────

@pytest.mark.parametrize("categoria", [
    "Supermercado",
    "Tienda de calzado",
    "Ferretería",
    "Clínica dental",
    "Restaurante",
    "Tienda de informática",
])
def test_unrelated_categories_rejected(categoria):
    assert category_matches("zapateria", categoria) is False


# ── otros términos de búsqueda ────────────────────────────────────────────────

def test_farmacia_accepted():
    assert category_matches("farmacia", "Farmacia") is True
    assert category_matches("farmacia", "Farmacias") is True


def test_farmacia_rejects_other():
    assert category_matches("farmacia", "Supermercado") is False


def test_restaurante_accepted():
    assert category_matches("restaurante", "Restaurante") is True
    assert category_matches("restaurantes", "Restaurante") is True


# ── search_category vacío → sin filtro ───────────────────────────────────────

def test_empty_search_term_always_passes():
    # stem("") = "" y "" es subcadena de cualquier string → acepta siempre.
    # En cli.py el guard `if search_category and ...` evita llamar aquí con "".
    assert category_matches("", "Supermercado") is True
    assert category_matches("", "Zapatería") is True
    assert category_matches("", "") is True
