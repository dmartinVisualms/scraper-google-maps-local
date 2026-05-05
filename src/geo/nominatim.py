from __future__ import annotations

import asyncio
import json
import logging
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

LOGGER = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "GoogleMapsScraper/1.0 (educational project)"

# Tipos OSM que consideramos "núcleo urbano" — preferidos sobre el polígono
# administrativo del municipio para acotar el grid.
URBAN_PLACE_TYPES = {"city", "town", "village", "hamlet", "suburb"}
POLYGON_GEOJSON_TYPES = {"Polygon", "MultiPolygon"}

# Radio (km) del bbox sintético cuando OSM sólo nos da un place=node sin polígono.
# El bbox se construye centrado en el centroide y se ajusta a la población:
# así en Ferrol (~65k) cubrimos casco + barrios, no el municipio completo con
# Cabo Prior, Doniños, etc.
URBAN_CORE_RADIUS_KM = (
    (15_000, 1.5),
    (40_000, 2.5),
    (110_000, 3.5),
    (float("inf"), 5.0),
)


def _radius_for_population(population: Optional[int]) -> float:
    """Devuelve el radio (km) del bbox sintético del núcleo urbano según
    población. Para población desconocida se usa 5 km (ciudad grande, conservador)."""
    if population is None or population <= 0:
        return 5.0
    for threshold, radius in URBAN_CORE_RADIUS_KM:
        if population < threshold:
            return radius
    return 5.0


def _bbox_around_point(lat: float, lon: float, radius_km: float) -> tuple:
    """Bbox cuadrado (min_lat, max_lat, min_lon, max_lon) de lado `2*radius_km`
    centrado en (lat, lon). Aproximación esférica suficiente para nuestro uso
    a escala de núcleos urbanos (<10 km de radio)."""
    dlat = radius_km / 111.0  # 1° lat ≈ 111 km
    cos_lat = math.cos(math.radians(lat))
    # cos cerca de los polos puede llegar a 0 — no aplicable aquí (España), pero
    # evitamos división por cero por robustez.
    dlon = radius_km / (111.0 * max(cos_lat, 0.01))
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


def _polygon_geojson_from_bbox(bbox: tuple) -> dict:
    """GeoJSON Polygon rectangular a partir de (min_lat, max_lat, min_lon, max_lon)."""
    min_lat, max_lat, min_lon, max_lon = bbox
    return {
        "type": "Polygon",
        "coordinates": [[
            [min_lon, min_lat],
            [max_lon, min_lat],
            [max_lon, max_lat],
            [min_lon, max_lat],
            [min_lon, min_lat],
        ]],
    }


def _select_place_node(results: list) -> Optional[dict]:
    """Devuelve el primer resultado `class=place` con `type` urbano —
    aceptamos también nodos sin polígono (para sintetizar el bbox después)."""
    for r in results:
        if (
            r.get("class") == "place"
            and r.get("type") in URBAN_PLACE_TYPES
            and r.get("lat") is not None
            and r.get("lon") is not None
        ):
            return r
    return None


def _city_name_from_query(city: str) -> str:
    """De 'Ferrol, A Coruña, España' devuelve 'Ferrol'."""
    return city.split(",")[0].strip()


def _country_from_query(city: str, default: str = "España") -> str:
    """Si la query incluye 'España'/'Spain' lo devuelve; si no, default."""
    parts = [p.strip() for p in city.split(",")]
    for p in parts[1:]:  # saltamos el nombre principal
        low = p.lower()
        if low in {"españa", "spain", "es"}:
            return p
    return default


@dataclass
class CityGeodata:
    display_name: str
    # bbox del polígono ADMIN — usado como red de seguridad
    # (min_lat, max_lat, min_lon, max_lon)
    bbox: tuple
    # Polígono administrativo del municipio — usado para FILTRAR refs scrapeados
    polygon_geojson: Optional[dict]
    # Polígono del núcleo urbano (si OSM lo tiene) — usado para CONSTRUIR el grid
    grid_polygon_geojson: Optional[dict] = None
    # bbox del núcleo (si difiere del admin)
    grid_bbox: Optional[tuple] = None
    # Origen del grid_polygon: "place" (núcleo urbano), "boundary" (cae al admin)
    grid_source: str = "boundary"


def _bbox_from_result(result: dict) -> tuple:
    bb = result["boundingbox"]  # [south, north, west, east] — strings
    return (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))


def _has_polygon(result: dict) -> bool:
    g = result.get("geojson") or {}
    return g.get("type") in POLYGON_GEOJSON_TYPES


def _select_grid_result(results: list) -> Optional[dict]:
    """Devuelve el primer resultado con `class=place` y type urbano que tenga
    un polígono GeoJSON, o None si ninguno cumple."""
    for r in results:
        if (
            r.get("class") == "place"
            and r.get("type") in URBAN_PLACE_TYPES
            and _has_polygon(r)
        ):
            return r
    return None


def _select_admin_result(results: list) -> Optional[dict]:
    """Devuelve el primer resultado con `class=boundary` administrativo y polígono."""
    for r in results:
        if (
            r.get("class") == "boundary"
            and r.get("type") == "administrative"
            and _has_polygon(r)
        ):
            return r
    return None


def _fetch_url_blocking(url: str) -> list:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "es"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def _query_nominatim_free(city: str) -> list:
    """Consulta libre `q=...`. Devuelve lo que tenga OSM mejor indexado."""
    params = urllib.parse.urlencode({
        "q": city,
        "format": "json",
        "polygon_geojson": "1",
        "limit": "10",
        "addressdetails": "0",
    })
    url = f"{NOMINATIM_URL}?{params}"
    LOGGER.debug("Nominatim (free): GET %s", url)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_url_blocking, url)


async def _query_nominatim_structured(city_name: str, country: str) -> list:
    """Consulta estructurada `city=X&country=Y`. Suele encontrar el `place=node`
    cuando la consulta libre sólo devuelve `boundary=administrative`."""
    params = urllib.parse.urlencode({
        "city": city_name,
        "country": country,
        "format": "json",
        "polygon_geojson": "1",
        "limit": "10",
        "addressdetails": "0",
    })
    url = f"{NOMINATIM_URL}?{params}"
    LOGGER.debug("Nominatim (structured): GET %s", url)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_url_blocking, url)


async def fetch_city_geodata(city: str, population: Optional[int] = None) -> CityGeodata:
    """Consulta Nominatim para obtener bbox y polígono(s) GeoJSON de una ciudad.

    Estrategia (en orden de prioridad para el grid):
      1. **place con polígono** (consulta libre) — núcleo urbano definido en OSM.
      2. **place=node con bbox sintético** (consulta estructurada de fallback) —
         si OSM solo tiene el centroide del núcleo como nodo, construimos un
         bbox cuadrado de radio = f(población) alrededor de él. Esto evita
         caer al término municipal entero para ciudades como Ferrol, Vigo o
         A Coruña, cuyo `place=town/city` figura sólo como nodo en OSM.
      3. **boundary=administrative** (último recurso) — término municipal
         completo. Comportamiento previo al fix de doble consulta.

    El polígono administrativo se conserva siempre como `polygon_geojson` (red
    de seguridad para filtrar negocios scrapeados, aunque caigan en barrios
    periféricos legítimos). El polígono del núcleo, si existe o se sintetiza,
    se devuelve adicionalmente como `grid_polygon_geojson` y se usará para
    acotar la generación del grid de sectores.

    `population` se usa para dimensionar el bbox sintético (paso 2). Si es
    None, se asume ciudad grande (radio máximo).
    """
    # ── Consulta libre ─────────────────────────────────────────────────
    data = await _query_nominatim_free(city)
    if not data:
        raise ValueError(
            f"Nominatim no encontró resultados para '{city}'. "
            "Prueba con un nombre más específico (ej: 'Madrid, España')."
        )

    # Admin amplio (red de seguridad para filtrar refs)
    admin = _select_admin_result(data) or data[0]
    admin_bbox = _bbox_from_result(admin)
    admin_geojson = admin.get("geojson")
    display_name = admin.get("display_name", city)

    # Paso 1: place con polígono en la consulta libre
    place = _select_grid_result(data)
    if place is not None:
        grid_bbox = _bbox_from_result(place)
        LOGGER.info(
            "Nominatim → %s | grid=place:%s con polígono "
            "(bbox lat=[%.4f,%.4f] lon=[%.4f,%.4f]) | admin=boundary:administrative",
            display_name, place.get("type", "?"),
            grid_bbox[0], grid_bbox[1], grid_bbox[2], grid_bbox[3],
        )
        return CityGeodata(
            display_name=display_name,
            bbox=admin_bbox,
            polygon_geojson=admin_geojson,
            grid_polygon_geojson=place.get("geojson"),
            grid_bbox=grid_bbox,
            grid_source="place",
        )

    # Paso 2: consulta estructurada para localizar el place=node (centroide)
    city_name = _city_name_from_query(city)
    country = _country_from_query(city)
    try:
        structured = await _query_nominatim_structured(city_name, country)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "Nominatim (structured) falló para %s/%s: %s — caigo al admin",
            city_name, country, exc,
        )
        structured = []

    # 2a) Si en la estructurada hay un place con polígono, también lo aceptamos
    place_struct = _select_grid_result(structured)
    if place_struct is not None:
        grid_bbox = _bbox_from_result(place_struct)
        LOGGER.info(
            "Nominatim → %s | grid=place:%s con polígono (consulta estructurada) "
            "(bbox lat=[%.4f,%.4f] lon=[%.4f,%.4f])",
            display_name, place_struct.get("type", "?"),
            grid_bbox[0], grid_bbox[1], grid_bbox[2], grid_bbox[3],
        )
        return CityGeodata(
            display_name=display_name,
            bbox=admin_bbox,
            polygon_geojson=admin_geojson,
            grid_polygon_geojson=place_struct.get("geojson"),
            grid_bbox=grid_bbox,
            grid_source="place_structured",
        )

    # 2b) Si sólo hay un place=node (sin polígono), bbox sintético por población
    place_node = _select_place_node(structured) or _select_place_node(data)
    if place_node is not None:
        plat = float(place_node["lat"])
        plon = float(place_node["lon"])
        radius_km = _radius_for_population(population)
        synth_bbox = _bbox_around_point(plat, plon, radius_km)
        synth_geojson = _polygon_geojson_from_bbox(synth_bbox)
        LOGGER.info(
            "Nominatim → %s | grid=place:%s NODE → bbox sintético r=%.1fkm "
            "(lat=[%.4f,%.4f] lon=[%.4f,%.4f]) | población=%s",
            display_name, place_node.get("type", "?"), radius_km,
            synth_bbox[0], synth_bbox[1], synth_bbox[2], synth_bbox[3],
            population if population is not None else "—",
        )
        return CityGeodata(
            display_name=display_name,
            bbox=admin_bbox,
            polygon_geojson=admin_geojson,
            grid_polygon_geojson=synth_geojson,
            grid_bbox=synth_bbox,
            grid_source=f"synthetic_radius_{radius_km:.1f}km",
        )

    # Paso 3: fallback admin (comportamiento previo)
    LOGGER.info(
        "Nominatim → %s | núcleo no disponible (ni polígono ni nodo place) — "
        "grid sobre término municipal (bbox lat=[%.4f,%.4f] lon=[%.4f,%.4f])",
        display_name,
        admin_bbox[0], admin_bbox[1], admin_bbox[2], admin_bbox[3],
    )
    return CityGeodata(
        display_name=display_name,
        bbox=admin_bbox,
        polygon_geojson=admin_geojson,
        grid_polygon_geojson=None,
        grid_bbox=None,
        grid_source="boundary",
    )
