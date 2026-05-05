# src/pipeline/csv_writer.py
from __future__ import annotations

import asyncio
import csv
import gc
import logging
from dataclasses import asdict
from pathlib import Path
from typing import List

from src.domain import BusinessRecord
from src.pipeline.dedupe import make_fallback_key, normalize_maps_url

LOGGER = logging.getLogger(__name__)

FIELDNAMES = [
    "nombre", "telefono", "direccion", "web", "rating",
    "categoria", "source_query", "retrieved_at_utc", "maps_url",
    "municipio_origen",
]


class StreamingCsvWriter:
    """
    Escribe registros de BusinessRecord en CSV de forma incremental.
    - Dedup en vuelo: no escribe duplicados aunque vengan de sectores distintos.
    - Thread-safe para asyncio: usa asyncio.Lock.
    - Llama a gc.collect() cada 20 sectores para mantener la RAM estable.
    """

    def __init__(self, path: str, max_records: int = 0, resume: bool = False) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._seen: set = set()
        self._sector_count = 0
        self._total_written = 0
        self._max_records = int(max_records or 0)

        if resume and self._path.exists():
            # Modo reanudar: NO sobrescribir el CSV existente; rehidratar dedup desde disco.
            self._rehydrate_from_csv()
        else:
            # Escribir cabecera al inicio
            with self._path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
                writer.writeheader()

    def _rehydrate_from_csv(self) -> None:
        """Lee el CSV existente y popula `_seen` + `_total_written` para evitar
        duplicados al reanudar. Reutiliza las claves de dedup canónicas."""
        from src.domain import BusinessRecord

        count = 0
        with self._path.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                # Construir un BusinessRecord parcial sólo para reusar make_fallback_key
                # (que usa nombre/direccion/telefono normalizados).
                rec = BusinessRecord(
                    nombre=row.get("nombre", ""),
                    telefono=row.get("telefono", ""),
                    direccion=row.get("direccion", ""),
                    web=row.get("web", ""),
                    rating=row.get("rating", ""),
                    categoria=row.get("categoria", ""),
                    source_query=row.get("source_query", ""),
                    retrieved_at_utc=row.get("retrieved_at_utc", ""),
                    maps_url=row.get("maps_url", ""),
                    municipio_origen=row.get("municipio_origen", ""),
                )
                if rec.maps_url:
                    self._seen.add(f"url:{normalize_maps_url(rec.maps_url)}")
                self._seen.add(f"fallback:{make_fallback_key(rec)}")
                count += 1
        self._total_written = count
        LOGGER.info(
            "CSV rehidratado desde %s: %d registros existentes (dedup activo)",
            self._path, count,
        )

    async def write_sector(self, records: List[BusinessRecord]) -> int:
        """
        Añade los registros al CSV (modo append), filtrando duplicados.
        Devuelve el número de registros nuevos escritos.
        """
        async with self._lock:
            written = 0
            with self._path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
                for record in records:
                    url_key = f"url:{normalize_maps_url(record.maps_url)}" if record.maps_url else None
                    fallback_key = f"fallback:{make_fallback_key(record)}"
                    if (url_key and url_key in self._seen) or fallback_key in self._seen:
                        continue
                    if url_key:
                        self._seen.add(url_key)
                    self._seen.add(fallback_key)
                    writer.writerow(asdict(record))
                    written += 1

            self._total_written += written
            self._sector_count += 1

            if self._sector_count % 20 == 0:
                gc.collect()
                LOGGER.debug(
                    "GC ejecutado en sector %d (total escrito: %d)",
                    self._sector_count, self._total_written,
                )

            return written

    async def write_record(self, record: BusinessRecord) -> bool:
        """
        Escribe un único registro al CSV inmediatamente (modo append).
        Devuelve True si fue escrito, False si era duplicado.
        Permite parar la ejecución en cualquier momento sin perder datos.
        """
        url_key = f"url:{normalize_maps_url(record.maps_url)}" if record.maps_url else None
        fallback_key = f"fallback:{make_fallback_key(record)}"
        async with self._lock:
            if self._max_records > 0 and self._total_written >= self._max_records:
                return False
            if (url_key and url_key in self._seen) or fallback_key in self._seen:
                return False
            if url_key:
                self._seen.add(url_key)
            self._seen.add(fallback_key)
            with self._path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
                writer.writerow(asdict(record))
            self._total_written += 1
            if self._total_written % 100 == 0:
                gc.collect()
            return True

    @property
    def total_written(self) -> int:
        return self._total_written

    @property
    def duplicates_skipped(self) -> int:
        return len(self._seen) - self._total_written

    @property
    def is_full(self) -> bool:
        """True si max_records > 0 y ya se ha alcanzado el cupo global."""
        return self._max_records > 0 and self._total_written >= self._max_records
