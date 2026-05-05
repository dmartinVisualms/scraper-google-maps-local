"""Tests del early-stop por saturación de municipio en _run_sectors_with_saturation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import src.cli as cli_module


class _FakeCsvWriter:
    def __init__(self):
        self.total_written = 0
        self.is_full = False


def _make_args(concurrency=2, dry_limit=3):
    return SimpleNamespace(concurrency=concurrency, dry_sector_limit=dry_limit)


def test_saturation_skips_remaining_sectors(monkeypatch):
    """Si N sectores consecutivos retornan 0, se abandona el resto del municipio."""
    sector_results = [3, 0, 0, 0, 5, 2]  # 3 secos consecutivos al inicio = saturación
    sectors = [object() for _ in sector_results]
    processed_indices = []

    async def fake_process_sector(label, sector, *a, **kw):
        idx = sectors.index(sector)
        processed_indices.append(idx)
        return sector_results[idx]

    monkeypatch.setattr(cli_module, "_process_sector", fake_process_sector)

    csv_writer = _FakeCsvWriter()
    args = _make_args(concurrency=1, dry_limit=3)

    asyncio.run(
        cli_module._run_sectors_with_saturation(
            sectors=sectors, label_prefix="Test", pool=None, query="q",
            csv_writer=csv_writer, args=args, metrics={},
            municipio_origen="", municipio_polygon=None,
            checkpoint=None, municipio_label="Test",
        )
    )
    # Debe haber procesado: 0 (got 3, reset), 1, 2, 3 (3 dry consecutivos → break)
    # El 4º (índice 3) entra ANTES de evaluar saturación porque va en lote.
    # Con concurrency=1, lote = 1 sector → tras index=3 (3 dry) → break.
    assert processed_indices == [0, 1, 2, 3]
    # Los sectores 4 y 5 NO deben haberse procesado.
    assert 4 not in processed_indices
    assert 5 not in processed_indices


def test_no_saturation_when_results_keep_coming(monkeypatch):
    """Si los sectores van añadiendo registros, no se aborta."""
    sector_results = [1, 0, 1, 0, 1, 1]  # alternancia → counter nunca llega a 3
    sectors = [object() for _ in sector_results]
    processed_indices = []

    async def fake_process_sector(label, sector, *a, **kw):
        idx = sectors.index(sector)
        processed_indices.append(idx)
        return sector_results[idx]

    monkeypatch.setattr(cli_module, "_process_sector", fake_process_sector)

    csv_writer = _FakeCsvWriter()
    args = _make_args(concurrency=1, dry_limit=3)

    asyncio.run(
        cli_module._run_sectors_with_saturation(
            sectors=sectors, label_prefix="Test", pool=None, query="q",
            csv_writer=csv_writer, args=args, metrics={},
            municipio_origen="", municipio_polygon=None,
            checkpoint=None, municipio_label="Test",
        )
    )
    assert processed_indices == [0, 1, 2, 3, 4, 5]


def test_dry_limit_zero_disables_early_stop(monkeypatch):
    """Con dry_sector_limit=0, nunca se abandona — todos los sectores procesados."""
    sector_results = [0] * 10
    sectors = [object() for _ in sector_results]
    processed_indices = []

    async def fake_process_sector(label, sector, *a, **kw):
        idx = sectors.index(sector)
        processed_indices.append(idx)
        return sector_results[idx]

    monkeypatch.setattr(cli_module, "_process_sector", fake_process_sector)

    csv_writer = _FakeCsvWriter()
    args = _make_args(concurrency=2, dry_limit=0)

    asyncio.run(
        cli_module._run_sectors_with_saturation(
            sectors=sectors, label_prefix="Test", pool=None, query="q",
            csv_writer=csv_writer, args=args, metrics={},
            municipio_origen="", municipio_polygon=None,
            checkpoint=None, municipio_label="Test",
        )
    )
    assert len(processed_indices) == 10


def test_csv_full_breaks_immediately(monkeypatch):
    """Si csv_writer.is_full, el bucle termina sin procesar más sectores."""
    sectors = [object() for _ in range(5)]
    processed_indices = []
    csv_writer = _FakeCsvWriter()

    async def fake_process_sector(label, sector, *a, **kw):
        idx = sectors.index(sector)
        processed_indices.append(idx)
        # Tras procesar el primero, marcar el writer como lleno
        csv_writer.is_full = True
        return 1

    monkeypatch.setattr(cli_module, "_process_sector", fake_process_sector)

    args = _make_args(concurrency=1, dry_limit=10)

    asyncio.run(
        cli_module._run_sectors_with_saturation(
            sectors=sectors, label_prefix="Test", pool=None, query="q",
            csv_writer=csv_writer, args=args, metrics={},
            municipio_origen="", municipio_polygon=None,
            checkpoint=None, municipio_label="Test",
        )
    )
    # Sólo debe haberse procesado 1 sector (el primero); luego is_full → break.
    assert processed_indices == [0]
