"""Casos reales de la revisión manual del ICP (prompt-mejora-analizador-icp.md).

Las webs están grabadas en tests/fixtures/icp/ (scripts/icp_record_fixtures.py),
así que el test no usa red.
"""
import asyncio
from pathlib import Path

import pytest

from src.analyzer.icp.fetch import make_replay_fetcher
from src.analyzer.icp.rules import UMBRAL_LLM, classify, load_marcas_config
from src.analyzer.icp.signals import extract_signals

FIX = Path(__file__).parent / "fixtures" / "icp"
CFG = load_marcas_config()

WEB_CASES = [
    # (fixture, nombre en Maps, web, tipo esperado)
    ("dandara", "Dándara", "http://www.dandara.es/", "marca_propia_franquicia"),
    ("marta_barcala", "Marta Barcala", "https://www.martabarcala.com/", "marca_propia"),
    ("muchas", "Muchas", "http://bit.ly/3olfSTb", "no_moda"),
    ("oruan", "O'ruan", "http://www.oruan.es/", "dudoso"),
    ("thebes", "Thebes", "http://www.thebesmoda.com/", "multimarca"),
]


@pytest.mark.parametrize("fixture,nombre,web,esperado", WEB_CASES)
def test_clasificacion_por_web(fixture, nombre, web, esperado):
    sig, html = asyncio.run(extract_signals(web, make_replay_fetcher(FIX / f"{fixture}.json.gz")))
    assert html, "la fixture no tiene la portada"
    c = classify(sig, nombre, CFG)
    assert c.tipo_negocio == esperado, (c.tipo_negocio, c.evidencias)
    assert c.evidencias
    if esperado != "dudoso":
        assert c.confianza >= UMBRAL_LLM  # no debe necesitar el paso con LLM


def test_thebes_detecta_marcas_y_tiendas():
    sig, _ = asyncio.run(extract_signals("http://www.thebesmoda.com/", make_replay_fetcher(FIX / "thebes.json.gz")))
    assert {"Selected", "Salsa", "Ecoalf", "Silbon", "Helly Hansen"} <= set(sig.marcas)
    assert sig.num_tiendas_web == 3 and sig.provincias == ["36"]


def test_dandara_tiendas_reales_no_las_del_csv():
    sig, _ = asyncio.run(extract_signals("http://www.dandara.es/", make_replay_fetcher(FIX / "dandara.json.gz")))
    assert sig.num_tiendas_web >= 35 and len(sig.provincias) > 10


# ── Casos que pasan por el LLM (respuesta real de Haiku grabada) ─────────────
import json  # noqa: E402

from src.analyzer.icp import llm  # noqa: E402


def _llm_fixture():
    return llm.parse_response((FIX / "llm_oruan_mva.json").read_text())


def test_oruan_sigue_dudoso_tras_llm():
    sig, _ = asyncio.run(extract_signals("http://www.oruan.es/", make_replay_fetcher(FIX / "oruan.json.gz")))
    final = llm.merge(classify(sig, "O'ruan", CFG), _llm_fixture()["oruan"])
    assert final.tipo_negocio == "dudoso"


def test_mi_vestidor_azul_sin_web_queda_en_revision():
    # Sin web y sin marcas en los fragmentos de Instagram: no hay datos para afirmar multimarca.
    snippets = json.loads((FIX / "mi_vestidor_azul_snippets.json").read_text())["snippets"]
    assert snippets
    final = llm.merge(classify(None, "Mi Vestidor Azul", CFG), _llm_fixture()["mva"])
    assert final.tipo_negocio == "dudoso"


def test_prompt_lleva_un_bloque_por_negocio():
    p = llm.build_prompt([{"id": "a", "nombre": "X", "texto": "t"}, {"id": "b", "nombre": "Y", "texto": "u"}])
    assert p.count("<negocio id=") == 2


def test_llm_con_confianza_alta_sustituye_a_reglas():
    reglas = classify(None, "Tienda X", CFG)
    final = llm.merge(reglas, llm.Clasificacion("multimarca", 0.9, ["etiqueta @vila y @object"], "llm"))
    assert final.tipo_negocio == "multimarca" and final.fuente == "llm"


def test_titulo_no_moda_gana_al_catalogo():
    from src.analyzer.icp.signals import Signals
    sig = Signals(web="x", final_url="https://ortopedia.x.com/", ok=True, title="Ortopedia y eliminación de barreras",
                  marcas=[f"Marca {i}" for i in range(57)], num_tiendas_web=10, sector_real="moda")
    assert classify(sig, "Centros Ortopédicos", CFG).tipo_negocio == "no_moda"


def test_cadena_sin_marcas_por_recuento_del_dataset():
    from src.analyzer.icp.signals import Signals
    sig = Signals(web="x", final_url="https://pilarprieto.es/", ok=True, title="Tienda online de moda mujer", sector_real="moda")
    assert classify(sig, "Pilar Prieto", CFG, n_tiendas_dataset=17).tipo_negocio == "marca_propia"
    assert classify(sig, "O'ruan", CFG, n_tiendas_dataset=5).tipo_negocio == "dudoso"
    assert classify(None, "Merkal Calzados", CFG, n_tiendas_dataset=9).tipo_negocio == "marca_propia"
