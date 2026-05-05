"""Orquestación de scraping encadenado por comunidad autónoma."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Dict, List, Optional

from src.comunidad.dataset import load_municipios
from src.pipeline.checkpoint import CheckpointStore, CityResumeState

LOGGER = logging.getLogger(__name__)


def build_municipio_queue(comunidad: str, min_poblacion: int) -> List[Dict]:
    """Devuelve la lista de municipios a procesar para una CCAA, ya filtrada y ordenada."""
    municipios = load_municipios(comunidad, min_poblacion)
    LOGGER.info(
        "Comunidad %s: %d municipios con población >= %d",
        comunidad, len(municipios), min_poblacion,
    )
    return municipios


# `process_city` puede aceptar (city_str, municipio_origen, poblacion) o
# además un kwarg `resume_state`. Para mantener retro-compatibilidad con tests
# y código antiguo, llamamos sin kwarg cuando no hay checkpoint.
ProcessCityFn = Callable[..., Awaitable[int]]


async def run_comunidad(
    comunidad: str,
    min_poblacion: int,
    process_city: ProcessCityFn,
    is_full: Callable[[], bool] = lambda: False,
    checkpoint: Optional[CheckpointStore] = None,
) -> int:
    """Ejecuta `process_city` para cada municipio de la CCAA.

    Si `is_full()` devuelve True después de un municipio, el bucle se detiene.
    Si `checkpoint` se proporciona, se saltan los municipios ya completados y
    se pasa el `resume_state` parcial al municipio en curso.
    """
    municipios = build_municipio_queue(comunidad, min_poblacion)
    total_municipios = len(municipios)
    total_records = 0

    completed = checkpoint.completed_municipios if checkpoint else set()

    for idx, m in enumerate(municipios, start=1):
        if is_full():
            LOGGER.info(
                "Cap global alcanzado tras %d/%d municipios — parando.",
                idx - 1, total_municipios,
            )
            break
        city_str = f"{m['nombre']}, {m['provincia']}, España"

        if city_str in completed:
            LOGGER.info(
                "[Municipio %d/%d] %s ⤳ Skip (resume): ya completado",
                idx, total_municipios, city_str,
            )
            continue

        LOGGER.info(
            "[Municipio %d/%d] %s (%d hab) ─────────────────",
            idx, total_municipios, city_str, m["poblacion"],
        )
        resume_state: Optional[CityResumeState] = (
            checkpoint.get_resume_state(city_str) if checkpoint else None
        )
        try:
            if checkpoint:
                written = await process_city(
                    city_str, m["nombre"], int(m["poblacion"]), resume_state=resume_state,
                )
            else:
                written = await process_city(city_str, m["nombre"], int(m["poblacion"]))
            total_records += written
            LOGGER.info(
                "[Municipio %d/%d] %s → %d nuevos válidos (acumulado: %d)",
                idx, total_municipios, m["nombre"], written, total_records,
            )
            if checkpoint:
                await checkpoint.mark_municipio_done(city_str)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(
                "[Municipio %d/%d] %s falló: %s — continuando con el siguiente",
                idx, total_municipios, m["nombre"], exc,
            )
    return total_records
