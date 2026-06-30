from __future__ import annotations

import pytest

from src.analyzer.scoring import (
    coords_from_maps_url,
    count_stores_by_brand,
    haversine_km,
    load_avatares,
    load_eci_locations,
    load_weights,
    nearest_eci_distance_km,
    normalize_brand_key,
    num_tiendas_for,
    score_avatar,
    score_distancia_eci,
    score_madurez_digital,
    score_num_tiendas,
    score_poblacion,
    compute_score,
    tramo_for_score,
)

# ─── Geometría ──────────────────────────────────────────────────────────

def test_haversine_madrid_barcelona():
    d = haversine_km(40.4168, -3.7038, 41.3851, 2.1734)
    assert 500 < d < 510


def test_coords_from_maps_url_valid():
    url = "https://www.google.com/maps/place/Foo/@43.21,-8.69,17z/data=abc"
    lat, lon = coords_from_maps_url(url)
    assert lat == pytest.approx(43.21)
    assert lon == pytest.approx(-8.69)


def test_coords_from_maps_url_missing():
    assert coords_from_maps_url("https://example.com/foo") == (None, None)
    assert coords_from_maps_url("") == (None, None)


def test_coords_from_maps_url_data_format():
    """Formato real de Google Maps tras click en place: data=!...!3dLAT!4dLON!..."""
    url = "https://www.google.com/maps/place/Foo/data=!4m7!3m6!1s0x123!8m2!3d42.234976!4d-8.716834!16s/abc"
    lat, lon = coords_from_maps_url(url)
    assert lat == pytest.approx(42.234976)
    assert lon == pytest.approx(-8.716834)


def test_nearest_eci_picks_closest():
    eci = [
        {"ciudad": "Vigo", "lat": 42.2333, "lon": -8.7163},
        {"ciudad": "A Coruña", "lat": 43.3499, "lon": -8.4123},
    ]
    # Carballo (~27km de A Coruña por haversine, ~110km de Vigo)
    d, name = nearest_eci_distance_km(43.21, -8.69, eci)
    assert name == "A Coruña"
    assert 20 < d < 35


# ─── Configs reales ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def real_weights():
    return load_weights()


@pytest.fixture(scope="module")
def real_avatars():
    return load_avatares()


@pytest.fixture(scope="module")
def real_eci():
    return load_eci_locations()


def test_real_configs_load_smoke(real_weights, real_avatars, real_eci):
    assert "criterios" in real_weights
    assert len(real_avatars) >= 3
    assert len(real_eci) >= 30


# ─── Scoring por criterio (con configs reales) ──────────────────────────

def test_score_distancia_eci_optimo(real_weights):
    pts, etiqueta = score_distancia_eci(60, real_weights)
    assert pts == 20
    assert "óptimo" in etiqueta


def test_score_distancia_eci_sombra(real_weights):
    pts, _ = score_distancia_eci(15, real_weights)
    assert pts == 10


def test_score_distancia_eci_aislado(real_weights):
    pts, _ = score_distancia_eci(150, real_weights)
    assert pts == 10


def test_score_poblacion_ideal(real_weights):
    pts, etiqueta = score_poblacion(25000, real_weights)
    assert pts == 15
    assert "ideal" in etiqueta


def test_score_poblacion_rural(real_weights):
    pts, _ = score_poblacion(3000, real_weights)
    assert pts == 3


def test_score_poblacion_urbano(real_weights):
    pts, etiqueta = score_poblacion(300000, real_weights)
    assert pts == 10
    assert "gran urbe" in etiqueta


def test_score_num_tiendas(real_weights):
    assert score_num_tiendas(4, real_weights)[0] == 30
    assert score_num_tiendas(2, real_weights)[0] == 15
    assert score_num_tiendas(1, real_weights)[0] == 5
    # Volumen crítico (central/franquicia) empata con la micro-cadena ideal
    assert score_num_tiendas(10, real_weights)[0] == 30


def test_score_madurez(real_weights):
    # Sin tecnología: valor base de ecommerce_funcional (10)
    assert score_madurez_digital("ecommerce_funcional", real_weights)[0] == 10
    # El negocio solo en redes puntúa por encima del ecommerce genérico (cliente premium)
    assert score_madurez_digital("solo_redes_sociales", real_weights)[0] == 15
    assert score_madurez_digital("sin_presencia", real_weights)[0] == 5
    # Con tecnología: WooCommerce/PrestaShop=20, Magento=15, Shopify=8
    assert score_madurez_digital("ecommerce_funcional", real_weights, "WooCommerce")[0] == 20
    assert score_madurez_digital("ecommerce_funcional", real_weights, "PrestaShop")[0] == 20
    assert score_madurez_digital("ecommerce_funcional", real_weights, "Magento")[0] == 15
    assert score_madurez_digital("ecommerce_funcional", real_weights, "Shopify")[0] == 8
    # Tecnología desconocida → cae al base
    assert score_madurez_digital("ecommerce_funcional", real_weights, "Velfix")[0] == 10


def test_tramo_for_score(real_weights):
    assert tramo_for_score(80, real_weights)[0] == "P1"
    assert tramo_for_score(60, real_weights)[0] == "P2"
    assert tramo_for_score(40, real_weights)[0] == "P3"
    assert tramo_for_score(10, real_weights)[0] == "P4"


# ─── Avatar matching ────────────────────────────────────────────────────

def test_score_avatar_match_avatar1(real_avatars, real_weights):
    ctx = {
        "poblacion": 25000,
        "distancia_eci_km": 60,
        "num_tiendas": 4,
        "ecommerce": True,
    }
    pts, etiqueta, av_id = score_avatar(ctx, real_avatars, real_weights)
    assert av_id == 1
    assert pts == 15
    assert "claro" in etiqueta


def test_score_avatar_partial(real_avatars, real_weights):
    # Encaje parcial sobre Avatar 3 (4 criterios, n=2): cumple población + distancia,
    # falla num_tiendas y ecommerce → 2/4 → parcial.
    ctx = {
        "poblacion": 250000,
        "distancia_eci_km": 5,
        "num_tiendas": 5,
        "ecommerce": False,
    }
    pts, etiqueta, av_id = score_avatar(ctx, real_avatars, real_weights)
    assert av_id == 3
    assert pts == 7
    assert "parcial" in etiqueta


def test_score_avatar_volume_node_avatar4(real_avatars, real_weights):
    # Central/franquicia: 50 tiendas con geo válida → Avatar 4 encaje claro.
    ctx = {
        "poblacion": 500,
        "distancia_eci_km": 200,
        "num_tiendas": 50,
        "ecommerce": False,
    }
    pts, etiqueta, av_id = score_avatar(ctx, real_avatars, real_weights)
    assert av_id == 4
    assert pts == 15
    assert "claro" in etiqueta


def test_score_avatar_no_match(real_avatars, real_weights):
    # Tienda única rural y aislada: no encaja con ningún avatar.
    ctx = {
        "poblacion": 500,
        "distancia_eci_km": 200,
        "num_tiendas": 1,
        "ecommerce": False,
    }
    pts, _, av_id = score_avatar(ctx, real_avatars, real_weights)
    assert pts == 0
    assert av_id is None


# ─── Conteo de tiendas ──────────────────────────────────────────────────

def test_normalize_brand_key():
    assert normalize_brand_key("Lolitamoda - Vigo (centro)") == "lolitamoda vigo"
    assert normalize_brand_key("Zapaterías 54") == "zapaterías"


def test_count_stores_groups_by_brand_minus_municipio():
    # Cadena real: mismo nombre + municipio distinto -> agrupa quitando el municipio
    rows = [
        {"nombre": "Lolitamoda Noia", "municipio_origen": "Noia"},
        {"nombre": "Lolitamoda Boiro", "municipio_origen": "Boiro"},
        {"nombre": "Lolitamoda Ribeira", "municipio_origen": "Ribeira"},
        {"nombre": "Casais Noia", "municipio_origen": "Noia"},
        {"nombre": "Casais Ribeira", "municipio_origen": "Ribeira"},
        {"nombre": "Único Concept Store", "municipio_origen": "Vigo"},
    ]
    counts = count_stores_by_brand(rows)
    assert counts["lolitamoda"] == 3
    assert counts["casais"] == 2
    assert counts["único concept store"] == 1


def test_distinct_shops_not_merged_by_first_word():
    # Regresión: antes 'calzados'/'calzado' colapsaban decenas de negocios en uno.
    rows = [
        {"nombre": "Calzados Raquel", "municipio_origen": "Lugo"},
        {"nombre": "Calzados Mara", "municipio_origen": "Foz"},
        {"nombre": "Calzado para Pies Especiales S.L.", "municipio_origen": "Vigo"},
    ]
    counts = count_stores_by_brand(rows)
    assert counts["calzados raquel"] == 1
    assert counts["calzados mara"] == 1
    assert num_tiendas_for("Calzado para Pies Especiales S.L.", counts, "Vigo") == 1


def test_num_tiendas_for_returns_count():
    rows = [
        {"nombre": "Lolitamoda Noia", "municipio_origen": "Noia"},
        {"nombre": "Lolitamoda Boiro", "municipio_origen": "Boiro"},
        {"nombre": "Lolitamoda Ribeira", "municipio_origen": "Ribeira"},
    ]
    counts = count_stores_by_brand(rows)
    assert num_tiendas_for("Lolitamoda Cee", counts, "Cee") == 3


def test_num_tiendas_for_unknown_returns_one():
    assert num_tiendas_for("Marca Desconocida XYZ", {}) == 1


# ─── Cálculo completo ───────────────────────────────────────────────────

def test_compute_score_microcadena_galicia(real_weights, real_avatars, real_eci):
    """Negocio en Carballo (~16km de ECI A Coruña, pob 31k), 4 tiendas, ecommerce funcional."""
    ctx = {
        "lat": 43.21, "lon": -8.69,
        "poblacion": 31000,
        "num_tiendas": 4,
        "madurez": "ecommerce_funcional",
        "tecnologia": "WooCommerce",
    }
    result = compute_score(ctx, eci_locations=real_eci, avatares=real_avatars, weights=real_weights)
    # Distancia ~16km → sombra ECI = 10pts. Pob 31k → ideal = 15. Tiendas 4 → 30.
    # Madurez ecommerce+Woo → 20. Avatar 1: pob ✓ dist ✗ tiendas ✓ (ecommerce no se evalúa) = 2/3 → no encaja.
    # Total = 10 + 15 + 30 + 20 + 0 = 75 → P1
    assert result["puntuacion_total"] == 75
    assert result["prioridad"] == "P1"
    assert "ECI" in result["justificacion"]
    assert "tiendas" in result["justificacion"]


def test_compute_score_p1_full_match(real_weights, real_avatars, real_eci):
    """Caso ideal Avatar 1: 60km de ECI, pob 25k, 4 tiendas, ecommerce → 25+15+25+20+15 = 100."""
    # Punto a ~60km al norte de Madrid (Castellana 40.447, -3.690): lat ~40.985
    ctx = {
        "lat": 40.985, "lon": -3.690,
        "poblacion": 25000,
        "num_tiendas": 4,
        "madurez": "ecommerce_funcional",
    }
    result = compute_score(ctx, eci_locations=real_eci, avatares=real_avatars, weights=real_weights)
    assert result["puntuacion_total"] >= 75  # P1
    assert result["prioridad"] == "P1"


def test_compute_score_no_coords(real_weights, real_avatars, real_eci):
    """Sin lat/lon → distancia=0pts."""
    ctx = {
        "lat": None, "lon": None,
        "poblacion": 25000,
        "num_tiendas": 4,
        "madurez": "ecommerce_funcional",
    }
    result = compute_score(ctx, eci_locations=real_eci, avatares=real_avatars, weights=real_weights)
    assert result["breakdown"]["distancia_eci"]["puntos"] == 0
    assert "sin coordenadas" in result["justificacion"]
