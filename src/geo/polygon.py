"""Utilidad reutilizable de filtrado punto-en-polígono.

Centraliza la lógica antes enterrada en `grid.filter_by_polygon` para que
pueda usarse también en el filtrado early de refs por coordenada.
"""
from __future__ import annotations

import logging
from typing import Optional

LOGGER = logging.getLogger(__name__)


def polygon_from_geojson(geojson: Optional[dict]):
    """Construye un Shapely geometry a partir de un GeoJSON.

    Devuelve None si:
      - el GeoJSON es None/vacío
      - shapely no está instalado
      - el GeoJSON no es parseable

    El llamante debe tratar None como "sin filtro" (passthrough).
    """
    if not geojson:
        return None
    try:
        from shapely.geometry import shape  # type: ignore
    except ImportError:
        LOGGER.warning("shapely no instalado — filtrado por polígono desactivado")
        return None
    try:
        return shape(geojson)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("GeoJSON inválido (%s) — sin filtro de polígono", exc)
        return None


def point_in_polygon(polygon, lat: float, lon: float) -> bool:
    """Devuelve True si (lat, lon) cae dentro del polígono Shapely.

    Si `polygon` es None se devuelve True (sin filtro), preservando el
    comportamiento de fallback cuando no hay GeoJSON disponible.
    """
    if polygon is None:
        return True
    try:
        from shapely.geometry import Point  # type: ignore
    except ImportError:
        return True
    return polygon.contains(Point(lon, lat))
