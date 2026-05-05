"""Tests para src.cli._filter_subdivisions — filtro de hijos de subdivisión.

Asegura que la subdivisión adaptativa NO arrastra la búsqueda a zonas no
urbanas: cuando un sector borde se subdivide, sus 4 hijos pueden quedar fuera
del polígono del núcleo y deben descartarse antes de procesarlos.
"""
from __future__ import annotations

from src.cli import _filter_subdivisions
from src.geo.grid import Sector
from src.geo.polygon import polygon_from_geojson


# Cuadrado pequeño centrado en (43.50, -8.25) con lado ~0.02° (~2 km)
SQUARE_GEOJSON = {
    "type": "Polygon",
    "coordinates": [[
        [-8.26, 43.49],
        [-8.24, 43.49],
        [-8.24, 43.51],
        [-8.26, 43.51],
        [-8.26, 43.49],
    ]],
}


def _children(parent_lat: float, parent_lon: float, cell_deg: float):
    """Reproduce el patrón de _subdivide: 4 hijos NW, NE, SW, SE."""
    half = cell_deg / 2
    quarter = half / 2
    return [
        Sector(lat=parent_lat + quarter, lon=parent_lon - quarter, zoom=17, cell_deg=half),
        Sector(lat=parent_lat + quarter, lon=parent_lon + quarter, zoom=17, cell_deg=half),
        Sector(lat=parent_lat - quarter, lon=parent_lon - quarter, zoom=17, cell_deg=half),
        Sector(lat=parent_lat - quarter, lon=parent_lon + quarter, zoom=17, cell_deg=half),
    ]


def test_passthrough_when_no_polygon():
    """Sin polígono no filtra (preserva comportamiento previo)."""
    children = _children(43.50, -8.25, cell_deg=0.01)
    out = _filter_subdivisions(children, grid_polygon=None, parent_label="test")
    assert out == children


def test_all_children_inside_kept():
    """Padre en el centro del polígono → 4 hijos dentro."""
    polygon = polygon_from_geojson(SQUARE_GEOJSON)
    children = _children(43.50, -8.25, cell_deg=0.005)  # hijos cell 0.0025°
    out = _filter_subdivisions(children, polygon, parent_label="test")
    assert len(out) == 4


def test_children_outside_dropped():
    """Padre lejos del polígono → todos los hijos fuera, lista vacía."""
    polygon = polygon_from_geojson(SQUARE_GEOJSON)
    children = _children(43.10, -8.10, cell_deg=0.01)  # claramente fuera
    out = _filter_subdivisions(children, polygon, parent_label="test")
    assert out == []


def test_edge_parent_drops_some_children():
    """Padre en el borde → algunos hijos quedan dentro y otros fuera."""
    polygon = polygon_from_geojson(SQUARE_GEOJSON)
    # Padre en el borde oeste del cuadrado: lon=-8.26, lat=43.50
    # Children centers: (43.5025, -8.2625), (43.5025, -8.2575),
    #                   (43.4975, -8.2625), (43.4975, -8.2575)
    # Algunos quedarán fuera (lon < -8.26).
    children = _children(43.50, -8.26, cell_deg=0.01)
    out = _filter_subdivisions(children, polygon, parent_label="test")
    assert 0 < len(out) < 4
