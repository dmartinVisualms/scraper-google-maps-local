"""Tests para src.geo.polygon — point_in_polygon y polygon_from_geojson."""
from __future__ import annotations

import pytest

from src.geo.polygon import point_in_polygon, polygon_from_geojson

# Cuadrado simple Galicia-like: (-9.3, 41.8) – (-6.7, 43.8)
SQUARE_GEOJSON = {
    "type": "Polygon",
    "coordinates": [[
        [-9.3, 41.8],
        [-6.7, 41.8],
        [-6.7, 43.8],
        [-9.3, 43.8],
        [-9.3, 41.8],
    ]],
}


def test_polygon_from_geojson_none_returns_none():
    assert polygon_from_geojson(None) is None
    assert polygon_from_geojson({}) is None


def test_polygon_from_geojson_invalid_returns_none():
    assert polygon_from_geojson({"foo": "bar"}) is None


def test_point_in_polygon_none_polygon_passthrough():
    # Sin polígono → True (sin filtro)
    assert point_in_polygon(None, 42.0, -8.0) is True


def test_point_in_polygon_inside():
    poly = polygon_from_geojson(SQUARE_GEOJSON)
    if poly is None:
        pytest.skip("shapely no disponible")
    # Santiago de Compostela ~ (42.88, -8.55) — dentro
    assert point_in_polygon(poly, 42.88, -8.55) is True


def test_point_in_polygon_outside():
    poly = polygon_from_geojson(SQUARE_GEOJSON)
    if poly is None:
        pytest.skip("shapely no disponible")
    # Madrid ~ (40.4, -3.7) — fuera
    assert point_in_polygon(poly, 40.4, -3.7) is False


def test_point_in_polygon_lon_lat_order():
    """Regresión: point_in_polygon recibe (lat, lon) pero internamente
    construye Point(lon, lat) para alinear con GeoJSON."""
    poly = polygon_from_geojson(SQUARE_GEOJSON)
    if poly is None:
        pytest.skip("shapely no disponible")
    # (lon=-8, lat=42) está dentro; pasarlos invertidos debe dar False
    assert point_in_polygon(poly, 42.0, -8.0) is True
    assert point_in_polygon(poly, -8.0, 42.0) is False
