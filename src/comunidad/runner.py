"""Orquestación de scraping encadenado por comunidad autónoma."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Dict, List

from src.comunidad.dataset import load_municipios

LOGGER = logging.getLogger(__name__)


def build_municipio_queue(comunidad: str, min_poblacion: int) -> List[Dict]:
    """Devuelve la lista de municipios a procesar para una CCAA, ya filtrada y ordenada."""
    municipios = load_municipios(comunidad, min_poblacion)
    LOGGER.info(
        "Comunidad %s: %d municipios con población >= %d",
        comunidad, len(municipios), min_poblacion,
    )
    return municipios


async def run_comunidad(
    comunidad: str,
    min_poblacion: int,
    process_city: Callable[[str, str, int], Awaitable[int]],
    is_full: Callable[[], bool] = lambda: False,
) -> int:
    """Ejecuta `process_city(city_str, municipio_origen, poblacion)` para cada municipio de la CCAA.

    Si `is_full()` devuelve True después de un municipio, el bucle se detiene.
    Devuelve el total acumulado de registros válidos. process_city debe encargarse
    de escribir al CSV (compartido) — este runner sólo orquesta el bucle secuencial.
    """
    municipios = build_municipio_queue(comunidad, min_poblacion)
    total_municipios = len(municipios)
    total_records = 0

    for idx, m in enumerate(municipios, start=1):
        if is_full():
            LOGGER.info(
                "Cap global alcanzado tras %d/%d municipios — parando.",
                idx - 1, total_municipios,
            )
            break
        city_str = f"{m['nombre']}, {m['provincia']}, España"
        LOGGER.info(
            "[Municipio %d/%d] %s (%d hab) ─────────────────",
            idx, total_municipios, city_str, m["poblacion"],
        )
        try:
            written = await process_city(city_str, m["nombre"], int(m["poblacion"]))
            total_records += written
            LOGGER.info(
                "[Municipio %d/%d] %s → %d nuevos válidos (acumulado: %d)",
                idx, total_municipios, m["nombre"], written, total_records,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(
                "[Municipio %d/%d] %s falló: %s — continuando con el siguiente",
                idx, total_municipios, m["nombre"], exc,
            )
    return total_records
