"""Tests para src.geo.nominatim — selección inteligente del grid_polygon.

Verifica que `fetch_city_geodata` prefiere el polígono `class=place` (núcleo
urbano) sobre el `class=boundary` (término municipal completo) y cae al admin
cuando el núcleo no está disponible.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from src.geo.nominatim import fetch_city_geodata


# Polígonos de prueba claramente distintos para poder discriminarlos por igualdad.
ADMIN_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-8.40, 43.40], [-8.10, 43.40], [-8.10, 43.55], [-8.40, 43.55], [-8.40, 43.40]]],
}
PLACE_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-8.27, 43.48], [-8.21, 43.48], [-8.21, 43.51], [-8.27, 43.51], [-8.27, 43.48]]],
}


def _admin_result() -> dict:
    return {
        "display_name": "Ferrol, A Coruña, Galicia",
        "class": "boundary",
        "type": "administrative",
        "boundingbox": ["43.40", "43.55", "-8.40", "-8.10"],
        "geojson": ADMIN_POLYGON,
    }


def _place_result() -> dict:
    return {
        "display_name": "Ferrol",
        "class": "place",
        "type": "city",
        "boundingbox": ["43.48", "43.51", "-8.27", "-8.21"],
        "geojson": PLACE_POLYGON,
    }


def _run(coro):
    return asyncio.run(coro)


def _patch_fetch(payload: list):
    """Devuelve un context manager que sustituye urlopen por uno que entrega `payload`."""

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    body = json.dumps(payload).encode("utf-8")

    def _fake_urlopen(req, timeout=20):  # noqa: ARG001
        return _Resp(body)

    return patch("src.geo.nominatim.urllib.request.urlopen", _fake_urlopen)


def test_prefers_place_polygon_when_available():
    """Si la respuesta trae admin + place con polígono, grid_source='place'
    y los polígonos admin/grid son distintos."""
    payload = [_admin_result(), _place_result()]
    with _patch_fetch(payload):
        result = _run(fetch_city_geodata("Ferrol"))
    assert result.grid_source == "place"
    assert result.grid_polygon_geojson == PLACE_POLYGON
    assert result.polygon_geojson == ADMIN_POLYGON
    assert result.grid_polygon_geojson != result.polygon_geojson
    assert result.grid_bbox == (43.48, 43.51, -8.27, -8.21)
    assert result.bbox == (43.40, 43.55, -8.40, -8.10)


def test_falls_back_to_boundary_when_no_place_with_polygon():
    """Si sólo hay admin (sin núcleo place con polígono), grid_source='boundary',
    grid_polygon_geojson es None y el comportamiento es el previo a la mejora."""
    payload = [_admin_result()]
    with _patch_fetch(payload):
        result = _run(fetch_city_geodata("MunicipioSinNucleo"))
    assert result.grid_source == "boundary"
    assert result.grid_polygon_geojson is None
    assert result.grid_bbox is None
    assert result.polygon_geojson == ADMIN_POLYGON


def test_place_without_polygon_is_ignored():
    """Un resultado place sin geojson no debe usarse como núcleo —
    se usa el admin."""
    place_no_geom = _place_result()
    place_no_geom["geojson"] = None
    payload = [_admin_result(), place_no_geom]
    with _patch_fetch(payload):
        result = _run(fetch_city_geodata("Ferrol"))
    assert result.grid_source == "boundary"
    assert result.grid_polygon_geojson is None


def test_place_town_type_also_selected():
    """type=town también cuenta como núcleo urbano (no sólo type=city)."""
    place_town = _place_result()
    place_town["type"] = "town"
    payload = [_admin_result(), place_town]
    with _patch_fetch(payload):
        result = _run(fetch_city_geodata("PuebloMediano"))
    assert result.grid_source == "place"
    assert result.grid_polygon_geojson == PLACE_POLYGON


def test_unrelated_place_type_ignored():
    """class=place pero type fuera del whitelist (e.g. 'hamlet' está incluido,
    pero algo como 'farm' no)."""
    farm = _place_result()
    farm["type"] = "farm"
    payload = [_admin_result(), farm]
    with _patch_fetch(payload):
        result = _run(fetch_city_geodata("Granja"))
    assert result.grid_source == "boundary"
    assert result.grid_polygon_geojson is None


# ── Helpers de bbox sintético ────────────────────────────────────────────────

from src.geo.nominatim import (  # noqa: E402
    _bbox_around_point,
    _city_name_from_query,
    _country_from_query,
    _polygon_geojson_from_bbox,
    _radius_for_population,
    _select_place_node,
)


def test_radius_for_population_thresholds():
    """Tabla población→radio: 1.5 / 2.5 / 3.5 / 5 km."""
    assert _radius_for_population(5_000) == 1.5
    assert _radius_for_population(14_999) == 1.5
    assert _radius_for_population(15_000) == 2.5
    assert _radius_for_population(39_999) == 2.5
    assert _radius_for_population(40_000) == 3.5
    assert _radius_for_population(109_999) == 3.5
    assert _radius_for_population(110_000) == 5.0
    assert _radius_for_population(500_000) == 5.0
    # Población desconocida → conservador (5 km)
    assert _radius_for_population(None) == 5.0
    assert _radius_for_population(0) == 5.0


def test_bbox_around_point_dimensions():
    """Bbox cuadrado de 2*radius_km de lado, centrado en el punto."""
    # Ferrol: 43.485, -8.233 con radio 3.5 km
    bbox = _bbox_around_point(43.485, -8.233, 3.5)
    min_lat, max_lat, min_lon, max_lon = bbox
    # 1° lat ≈ 111 km → 3.5 km ≈ 0.0315°. Margen de 0.001°.
    dlat = (max_lat - min_lat) / 2
    assert abs(dlat - 3.5 / 111.0) < 1e-4
    # 1° lon a esta latitud ≈ 111 * cos(43.485°) ≈ 80.6 km
    # 3.5 km ≈ 0.0434°
    dlon = (max_lon - min_lon) / 2
    expected_dlon = 3.5 / (111.0 * 0.7257)  # cos(43.485°) ≈ 0.7257
    assert abs(dlon - expected_dlon) < 1e-3
    # Centrado:
    assert abs((min_lat + max_lat) / 2 - 43.485) < 1e-9
    assert abs((min_lon + max_lon) / 2 - (-8.233)) < 1e-9


def test_polygon_geojson_from_bbox():
    """GeoJSON Polygon válido cerrado (5 puntos, primero = último)."""
    bbox = (43.0, 43.5, -8.5, -8.0)
    geojson = _polygon_geojson_from_bbox(bbox)
    assert geojson["type"] == "Polygon"
    coords = geojson["coordinates"][0]
    assert len(coords) == 5
    assert coords[0] == coords[-1]  # cerrado
    # Vértices en orden: SW, SE, NE, NW, SW (lon, lat)
    assert coords[0] == [-8.5, 43.0]
    assert coords[2] == [-8.0, 43.5]


def test_city_name_from_query():
    assert _city_name_from_query("Ferrol, A Coruña, España") == "Ferrol"
    assert _city_name_from_query("Madrid") == "Madrid"
    assert _city_name_from_query(" Vigo ,  Pontevedra ") == "Vigo"


def test_country_from_query_detects_spain_variants():
    assert _country_from_query("Ferrol, A Coruña, España") == "España"
    assert _country_from_query("Madrid, Spain") == "Spain"
    assert _country_from_query("Vigo") == "España"  # default


def test_select_place_node_accepts_node_without_polygon():
    """Un place=town node sin geojson polígono debe ser seleccionado por
    `_select_place_node` (a diferencia de `_select_grid_result` que exige polígono)."""
    node = {
        "class": "place", "type": "town", "osm_type": "node",
        "lat": "43.4846", "lon": "-8.2330",
        "geojson": {"type": "Point", "coordinates": [-8.233, 43.485]},
    }
    assert _select_place_node([node]) is node
    # Pero `_select_grid_result` (que exige Polygon) lo rechaza
    from src.geo.nominatim import _select_grid_result
    assert _select_grid_result([node]) is None


# ── Doble consulta + bbox sintético: caso Ferrol ─────────────────────────────

def _patch_dual_fetch(free_payload: list, structured_payload: list):
    """Mock de Nominatim que distingue entre la consulta libre (q=...) y la
    estructurada (city=...&country=...)."""

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _fake_urlopen(req, timeout=20):  # noqa: ARG001
        # req.full_url contiene la URL completa
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if "city=" in url:
            body = json.dumps(structured_payload).encode("utf-8")
        else:
            body = json.dumps(free_payload).encode("utf-8")
        return _Resp(body)

    return patch("src.geo.nominatim.urllib.request.urlopen", _fake_urlopen)


def test_synthetic_bbox_when_only_node_available():
    """Caso Ferrol: la consulta libre no tiene `place=poly`, la estructurada
    devuelve un `place=town node`. Debe sintetizar un bbox alrededor del nodo
    con radio según población."""
    free = [_admin_result()]  # solo admin
    place_node = {
        "class": "place", "type": "town", "osm_type": "node",
        "lat": "43.4845713", "lon": "-8.2329968",
        "geojson": {"type": "Point", "coordinates": [-8.233, 43.485]},
        "boundingbox": ["43.4846", "43.4846", "-8.2330", "-8.2330"],
    }
    structured = [_admin_result(), place_node]
    with _patch_dual_fetch(free, structured):
        result = _run(fetch_city_geodata("Ferrol, A Coruña, España", population=65000))
    assert result.grid_source.startswith("synthetic_radius_")
    # Para 65k → radio 3.5 km → bbox de ~7×7 km centrado en (43.485, -8.233)
    assert "3.5" in result.grid_source
    assert result.grid_polygon_geojson is not None
    assert result.grid_polygon_geojson["type"] == "Polygon"
    # Centro del bbox sintético ≈ centroide del nodo
    min_lat, max_lat, min_lon, max_lon = result.grid_bbox
    assert abs((min_lat + max_lat) / 2 - 43.4845713) < 1e-4
    assert abs((min_lon + max_lon) / 2 - (-8.2329968)) < 1e-4
    # Polígono ADMIN sigue siendo el original
    assert result.polygon_geojson == ADMIN_POLYGON


def test_synthetic_bbox_uses_population_radius():
    """Población distinta → radio distinto."""
    free = [_admin_result()]
    place_node = {
        "class": "place", "type": "town", "osm_type": "node",
        "lat": "43.0", "lon": "-8.0",
        "geojson": {"type": "Point", "coordinates": [-8.0, 43.0]},
        "boundingbox": ["43.0", "43.0", "-8.0", "-8.0"],
    }
    structured = [_admin_result(), place_node]

    # Pueblo pequeño <15k → 1.5 km
    with _patch_dual_fetch(free, structured):
        small = _run(fetch_city_geodata("Pueblo, Provincia, España", population=8000))
    assert "1.5" in small.grid_source

    # Ciudad grande >110k → 5 km
    with _patch_dual_fetch(free, structured):
        big = _run(fetch_city_geodata("Ciudad, Provincia, España", population=300000))
    assert "5.0" in big.grid_source


def test_falls_back_to_admin_when_no_place_anywhere():
    """Si ni la libre ni la estructurada devuelven place=*, cae al admin
    (comportamiento previo). grid_source='boundary'."""
    free = [_admin_result()]
    structured = [_admin_result()]  # admin sin place ni node
    with _patch_dual_fetch(free, structured):
        result = _run(fetch_city_geodata("CentroSinPlace, X, España", population=20000))
    assert result.grid_source == "boundary"
    assert result.grid_polygon_geojson is None
    assert result.grid_bbox is None


def test_structured_query_returns_place_with_polygon():
    """Si la consulta estructurada SÍ devuelve un place=town con polígono,
    se prefiere antes que sintetizar un bbox."""
    free = [_admin_result()]
    place_with_poly = _place_result()  # place=city con PLACE_POLYGON
    structured = [_admin_result(), place_with_poly]
    with _patch_dual_fetch(free, structured):
        result = _run(fetch_city_geodata("CentroPoly, X, España", population=50000))
    assert result.grid_source == "place_structured"
    assert result.grid_polygon_geojson == PLACE_POLYGON
