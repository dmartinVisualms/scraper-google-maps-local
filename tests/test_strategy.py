"""Tests para src.cli._select_strategy — selector de estrategia híbrida."""
from __future__ import annotations

from src.cli import _select_strategy


# ── mode='auto' ──────────────────────────────────────────────────────────────

def test_auto_small_population_text_only():
    s = _select_strategy(5_000, "auto")
    assert s == {"text": True, "grid": None}


def test_auto_threshold_low_text_only():
    # 14_999 < 15_000 → text
    s = _select_strategy(14_999, "auto")
    assert s["text"] is True
    assert s["grid"] is None


def test_auto_medium_population_text_plus_soft_grid():
    s = _select_strategy(25_000, "auto")
    assert s["text"] is True
    assert s["grid"] == {"zoom": 14, "cell_deg": 0.01}


def test_auto_threshold_medium_upper():
    # 39_999 < 40_000 → text + soft grid
    s = _select_strategy(39_999, "auto")
    assert s["text"] is True
    assert s["grid"]["cell_deg"] == 0.01


def test_auto_large_population_grid_only():
    s = _select_strategy(80_000, "auto")
    assert s == {"text": False, "grid": {"zoom": 16, "cell_deg": 0.003}}


def test_auto_no_population_falls_back_to_grid():
    """Si no hay población, asumir grande para no perder cobertura."""
    s = _select_strategy(None, "auto")
    assert s == {"text": False, "grid": {"zoom": 16, "cell_deg": 0.003}}


# ── mode='text' ──────────────────────────────────────────────────────────────

def test_text_mode_overrides_population_small():
    assert _select_strategy(5_000, "text") == {"text": True, "grid": None}


def test_text_mode_overrides_population_large():
    assert _select_strategy(500_000, "text") == {"text": True, "grid": None}


def test_text_mode_no_population():
    assert _select_strategy(None, "text") == {"text": True, "grid": None}


# ── mode='grid' ──────────────────────────────────────────────────────────────

def test_grid_mode_small_population():
    s = _select_strategy(3_000, "grid")
    assert s == {"text": False, "grid": {"zoom": 16, "cell_deg": 0.003}}


def test_grid_mode_large_population():
    s = _select_strategy(500_000, "grid")
    assert s == {"text": False, "grid": {"zoom": 16, "cell_deg": 0.003}}


def test_grid_mode_no_population():
    assert _select_strategy(None, "grid") == {"text": False, "grid": {"zoom": 16, "cell_deg": 0.003}}
