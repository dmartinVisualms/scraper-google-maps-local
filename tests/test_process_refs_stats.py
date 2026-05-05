"""Tests para src.cli._process_refs — desglose retornado por sector.

Objetivo: garantizar que `_process_refs` devuelve un `local_stats` correcto con
contadores separados de procesados, errores, sin nombre, fuera de polígono y
fuera de categoría. La función `_log_refs_breakdown` se apoya en este dict para
emitir un log INFO inteligible cuando un sector descubre refs pero no escribe
ninguno al CSV.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional
from unittest.mock import patch

from src.cli import _process_refs, _log_refs_breakdown
from src.domain import BusinessRecord
from src.geo.polygon import polygon_from_geojson
from src.scraper.maps_search import SearchResultRef


# Polígono cuadrado para distinguir "dentro" / "fuera"
SQUARE_GEOJSON = {
    "type": "Polygon",
    "coordinates": [[
        [-8.30, 43.45],
        [-8.20, 43.45],
        [-8.20, 43.55],
        [-8.30, 43.55],
        [-8.30, 43.45],
    ]],
}


def _ref(name: str, lat: float, lon: float) -> SearchResultRef:
    """Construye un ref cuya maps_url contiene las coordenadas — coords_from_maps_url
    las podrá extraer."""
    return SearchResultRef(
        name=name,
        maps_url=f"https://www.google.com/maps/place/{name}/@{lat},{lon},17z/data=foo",
    )


def _record(
    nombre: str,
    lat: float,
    lon: float,
    categoria: str = "Zapatería",
) -> BusinessRecord:
    return BusinessRecord(
        nombre=nombre,
        telefono="",
        direccion="",
        web="",
        rating="",
        categoria=categoria,
        source_query="",
        retrieved_at_utc="",
        maps_url=f"https://www.google.com/maps/place/{nombre}/@{lat},{lon},17z",
    )


class _FakeCsvWriter:
    """Mínimo subset de StreamingCsvWriter para los tests."""

    def __init__(self, max_records: int = 0):
        self._records: List[BusinessRecord] = []
        self._max = max_records

    async def write_record(self, record: BusinessRecord) -> bool:
        if self._max > 0 and len(self._records) >= self._max:
            return False
        self._records.append(record)
        return True

    @property
    def total_written(self) -> int:
        return len(self._records)

    @property
    def is_full(self) -> bool:
        return self._max > 0 and len(self._records) >= self._max


class _FakePage:
    """Page falsa — `_process_refs` sólo llama a `goto`, que ignoramos."""

    async def goto(self, *_args, **_kwargs):
        return None


def _run(coro):
    return asyncio.run(coro)


def _scripted_extract(records_or_excs):
    """Devuelve un fake `extract_business_record` que entrega records/excepciones
    en orden. retry_async llama a la lambda hasta 2 veces; para excepciones, usamos
    un contador para que se "agote" y propague."""
    iter_state = {"i": 0}

    async def fake_extract(_page, _query):
        i = iter_state["i"]
        iter_state["i"] += 1
        item = records_or_excs[i]
        if isinstance(item, Exception):
            raise item
        return item

    return fake_extract


def _patch_dependencies(extract_fn):
    """Aplica los patches necesarios: extracción, navegación y retry pasivo."""

    async def passthrough_retry(func, attempts=2, base_delay=0.0):  # noqa: ARG001
        # Ejecuta la función sin reintentos para tests deterministas
        return await func()

    return [
        patch("src.cli.extract_business_record", extract_fn),
        patch("src.cli.retry_async", passthrough_retry),
        patch("src.cli.asyncio.sleep", lambda *_a, **_k: _noop()),
    ]


async def _noop():
    return None


def _run_with_patches(extract_fn, refs, polygon, search_category="Zapatería"):
    metrics = {
        "discovered": 0, "processed": 0, "errors": 0,
        "heuristic_stops": 0, "filtered_out_of_polygon": 0,
        "filtered_out_of_category": 0,
    }
    csv_writer = _FakeCsvWriter()
    page = _FakePage()

    patches = _patch_dependencies(extract_fn)
    for p in patches:
        p.start()
    try:
        local_stats = _run(_process_refs(
            refs=refs,
            page=page,
            query="zapatería en Test",
            slow_ms=0,
            sector_label="test",
            sector=None,
            csv_writer=csv_writer,
            metrics=metrics,
            municipio_origen="",
            municipio_polygon=polygon,
            search_category=search_category,
        ))
    finally:
        for p in patches:
            p.stop()
    return local_stats, metrics, csv_writer


# ── Tests ────────────────────────────────────────────────────────────────────

def test_returns_dict_with_expected_keys():
    """El contrato: _process_refs SIEMPRE devuelve un dict con las 5 claves."""
    extract = _scripted_extract([_record("A", 43.50, -8.25)])
    refs = [_ref("A", 43.50, -8.25)]
    local_stats, _, _ = _run_with_patches(extract, refs, polygon=None)
    assert set(local_stats.keys()) == {
        "processed", "errors", "no_name", "filtered_polygon", "filtered_category",
    }


def test_all_inside_polygon_and_matching_category():
    """3 records dentro del polígono y categoría OK → processed=3, resto=0."""
    polygon = polygon_from_geojson(SQUARE_GEOJSON)
    records = [
        _record("A", 43.50, -8.25, "Zapatería"),
        _record("B", 43.51, -8.24, "Zapatería"),
        _record("C", 43.49, -8.26, "Zapatería"),
    ]
    refs = [_ref(r.nombre, 43.50, -8.25) for r in records]
    extract = _scripted_extract(records)
    local_stats, _, csv_writer = _run_with_patches(extract, refs, polygon)
    assert local_stats["processed"] == 3
    assert local_stats["filtered_polygon"] == 0
    assert local_stats["filtered_category"] == 0
    assert local_stats["errors"] == 0
    assert local_stats["no_name"] == 0
    assert csv_writer.total_written == 3


def test_records_outside_polygon_are_filtered():
    """2 dentro + 1 fuera → processed=2, filtered_polygon=1."""
    polygon = polygon_from_geojson(SQUARE_GEOJSON)
    records = [
        _record("Inside1", 43.50, -8.25),
        _record("Outside", 43.10, -8.10),  # fuera del cuadrado
        _record("Inside2", 43.51, -8.24),
    ]
    refs = [_ref(r.nombre, 43.50, -8.25) for r in records]
    extract = _scripted_extract(records)
    local_stats, metrics, _ = _run_with_patches(extract, refs, polygon)
    assert local_stats["processed"] == 2
    assert local_stats["filtered_polygon"] == 1
    assert metrics["filtered_out_of_polygon"] == 1


def test_records_with_wrong_category_are_filtered():
    """Categoría no coincide → filtered_category cuenta, processed no."""
    polygon = polygon_from_geojson(SQUARE_GEOJSON)
    records = [
        _record("OK", 43.50, -8.25, "Zapatería"),
        _record("Wrong", 43.50, -8.25, "Restaurante"),
    ]
    refs = [_ref(r.nombre, 43.50, -8.25) for r in records]
    extract = _scripted_extract(records)
    local_stats, metrics, _ = _run_with_patches(
        extract, refs, polygon, search_category="zapatería",
    )
    assert local_stats["processed"] == 1
    assert local_stats["filtered_category"] == 1
    assert metrics["filtered_out_of_category"] == 1


def test_record_without_name_increments_no_name():
    """Si extract devuelve un record con nombre vacío, no_name +=1, processed=0."""
    polygon = polygon_from_geojson(SQUARE_GEOJSON)
    records = [
        _record("", 43.50, -8.25),  # sin nombre
        _record("OK", 43.50, -8.25),
    ]
    refs = [_ref("a", 43.50, -8.25), _ref("b", 43.50, -8.25)]
    extract = _scripted_extract(records)
    local_stats, _, _ = _run_with_patches(extract, refs, polygon)
    assert local_stats["no_name"] == 1
    assert local_stats["processed"] == 1


def test_extraction_exceptions_count_as_errors():
    """Excepción en extract → errors +=1."""
    polygon = polygon_from_geojson(SQUARE_GEOJSON)
    items = [
        _record("OK", 43.50, -8.25),
        RuntimeError("fallo simulado"),
    ]
    refs = [_ref("a", 43.50, -8.25), _ref("b", 43.50, -8.25)]
    extract = _scripted_extract(items)
    local_stats, metrics, _ = _run_with_patches(extract, refs, polygon)
    assert local_stats["processed"] == 1
    assert local_stats["errors"] == 1
    assert metrics["errors"] >= 1


def test_combined_breakdown():
    """Caso compuesto del plan: 5 records con 2 fuera del polígono y 1 categoría
    errónea → processed=2, filtered_polygon=2, filtered_category=1."""
    polygon = polygon_from_geojson(SQUARE_GEOJSON)
    records = [
        _record("In1", 43.50, -8.25, "Zapatería"),       # ok
        _record("Out1", 43.10, -8.10, "Zapatería"),      # fuera polígono
        _record("In2", 43.51, -8.24, "Zapatería"),       # ok
        _record("Out2", 43.10, -8.10, "Zapatería"),      # fuera polígono
        _record("WrongCat", 43.50, -8.25, "Bar"),        # categoría no coincide
    ]
    refs = [_ref("a", 43.50, -8.25)] * 5
    extract = _scripted_extract(records)
    local_stats, _, _ = _run_with_patches(
        extract, refs, polygon, search_category="zapatería",
    )
    assert local_stats == {
        "processed": 2,
        "filtered_polygon": 2,
        "filtered_category": 1,
        "errors": 0,
        "no_name": 0,
    }


# ── Test del helper de logging (Parte B.2) ───────────────────────────────────

def test_log_breakdown_no_op_when_all_written(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="src.cli")
    _log_refs_breakdown("test", discovered=3, written=3, local_stats={
        "processed": 3, "errors": 0, "no_name": 0,
        "filtered_polygon": 0, "filtered_category": 0,
    })
    # No debe loguear nada cuando written >= discovered
    assert not any("→ 0 al CSV" in r.message or "→ 3 al CSV" in r.message
                   for r in caplog.records)


def test_log_breakdown_all_filtered_emits_warning_log(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="src.cli")
    _log_refs_breakdown("Ferrol|3", discovered=12, written=0, local_stats={
        "processed": 0, "errors": 1, "no_name": 0,
        "filtered_polygon": 8, "filtered_category": 3,
    })
    msgs = [r.message for r in caplog.records]
    joined = " ".join(msgs)
    assert "12 descubiertos → 0 al CSV" in joined
    assert "8 fuera de polígono" in joined
    assert "3 categoría no coincide" in joined
    assert "1 errores" in joined


def test_log_breakdown_partial_emits_intermediate_log(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="src.cli")
    _log_refs_breakdown("test", discovered=10, written=4, local_stats={
        "processed": 4, "errors": 1, "no_name": 0,
        "filtered_polygon": 4, "filtered_category": 1,
    })
    msgs = " ".join(r.message for r in caplog.records)
    assert "10 descubiertos → 4 al CSV" in msgs
    assert "4 filtrados pol." in msgs
