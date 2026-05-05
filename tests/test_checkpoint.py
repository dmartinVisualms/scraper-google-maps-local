"""Tests del sistema de checkpoint y reanudación."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.domain import BusinessRecord
from src.pipeline.checkpoint import CheckpointStore, sector_key
from src.pipeline.csv_writer import StreamingCsvWriter


# ── CheckpointStore ────────────────────────────────────────────────────────


def test_sector_key_rounding():
    assert sector_key(42.10625001, -8.74375, 0.0050000001) == sector_key(
        42.10625, -8.74375, 0.005,
    )


def test_create_and_load_roundtrip(tmp_path: Path):
    ckpt = tmp_path / "job.checkpoint.json"
    store = CheckpointStore.create(
        ckpt,
        args={"category": "tiendas", "comunidad": "Galicia", "min_poblacion": 5000},
        output_csv="out/test.csv",
        job_id="abc",
    )
    asyncio.run(store.start_municipio("Vigo, Pontevedra, España", 295000))
    asyncio.run(store.mark_text_done("Vigo, Pontevedra, España"))
    asyncio.run(store.mark_sector_done(
        "Vigo, Pontevedra, España", 42.123, -8.123, 0.005, mode="leaf",
    ))
    asyncio.run(store.mark_sector_done(
        "Vigo, Pontevedra, España", 42.456, -8.456, 0.005, mode="subdivided",
    ))

    loaded = CheckpointStore.load(ckpt)
    assert loaded.args["category"] == "tiendas"
    assert loaded.output_csv == "out/test.csv"
    state = loaded.get_resume_state("Vigo, Pontevedra, España")
    assert state is not None
    assert state.text_done is True
    assert loaded.sector_state(sector_key(42.123, -8.123, 0.005)) == "leaf"
    assert loaded.sector_state(sector_key(42.456, -8.456, 0.005)) == "subdivided"


def test_mark_municipio_done_clears_current(tmp_path: Path):
    ckpt = tmp_path / "job.checkpoint.json"
    store = CheckpointStore.create(ckpt, args={}, output_csv="out/x.csv")
    label = "Foo, Bar, España"
    asyncio.run(store.start_municipio(label, 1000))
    asyncio.run(store.mark_municipio_done(label))

    loaded = CheckpointStore.load(ckpt)
    assert label in loaded.completed_municipios
    assert loaded.get_resume_state(label) is None


def test_atomic_write_no_partial_file(tmp_path: Path):
    ckpt = tmp_path / "job.checkpoint.json"
    store = CheckpointStore.create(ckpt, args={"k": "v"}, output_csv="out/x.csv")
    asyncio.run(store.mark_municipio_done("X, Y, Z"))
    # No debe quedar fichero temporal sin renombrar.
    assert not (tmp_path / "job.checkpoint.json.tmp").exists()
    # JSON debe ser parseable.
    json.loads(ckpt.read_text())


# ── StreamingCsvWriter resume / rehydrate ──────────────────────────────────


def _record(n: int, url: str = "") -> BusinessRecord:
    return BusinessRecord(
        nombre=f"Negocio {n}",
        telefono=f"60000000{n}",
        direccion=f"Calle {n}",
        web="",
        rating="",
        categoria="",
        source_query="",
        retrieved_at_utc="",
        maps_url=url or f"https://maps.google.com/p/{n}",
    )


def test_rehydrate_skips_existing_records(tmp_path: Path):
    """Tras crash + resume, no se vuelven a escribir registros previos."""
    csv_path = str(tmp_path / "out.csv")

    async def first_run():
        w = StreamingCsvWriter(csv_path, max_records=0)
        for i in range(5):
            await w.write_record(_record(i))
        return w.total_written

    async def second_run():
        w = StreamingCsvWriter(csv_path, max_records=0, resume=True)
        # Reintentar los mismos registros
        for i in range(5):
            written = await w.write_record(_record(i))
            assert written is False, f"registro {i} no debería re-escribirse"
        # Y añadir uno nuevo
        new_written = await w.write_record(_record(99))
        assert new_written is True
        return w.total_written

    n1 = asyncio.run(first_run())
    assert n1 == 5
    n2 = asyncio.run(second_run())
    assert n2 == 6  # 5 previos + 1 nuevo

    # El CSV final tiene 6 filas de datos + 1 cabecera
    lines = Path(csv_path).read_text().strip().splitlines()
    assert len(lines) == 7  # 1 header + 6 records
