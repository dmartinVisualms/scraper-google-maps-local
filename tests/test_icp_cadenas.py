"""Etiqueta CADENA: 8+ tiendas o en config/cadenas.json (sin red: filas sin web, DataForSEO a $0)."""
import argparse
import asyncio

from src.analyzer.cli import analyze


def _args():
    return argparse.Namespace(brands_path="config/excluded_brands.json", refresh=False,
                              no_llm=True, dataforseo_max_usd=0.0)


def test_etiqueta_cadena_por_recuento_y_por_lista():
    rows = [{"nombre": "Moda Zeta", "municipio_origen": f"Pueblo{i}", "direccion": f"calle {i}", "web": ""}
            for i in range(9)]
    rows += [{"nombre": "Courir", "municipio_origen": "Vigo", "direccion": "x", "web": ""},
             {"nombre": "Tienda Única", "municipio_origen": "Lugo", "direccion": "y", "web": ""}]
    out = asyncio.run(analyze(rows, _args()))
    by = {r["nombre"]: r for r in out}
    assert by["Moda Zeta"]["etiqueta"] == "CADENA (9 tiendas)"
    assert by["Courir"]["etiqueta"].startswith("CADENA")
    assert by["Tienda Única"]["etiqueta"] == ""
    assert all(r["prioridad"] != "P1" for r in out if r["etiqueta"])


def test_agrupar_cadenas_une_variantes():
    from scripts.icp_rescore_report import agrupar_cadenas
    r = lambda n, m, w="": {"nombre": n, "municipio_origen": m, "web": w}
    rows = [r("Muchas Noia", "Noia", "http://bit.ly/3olfSTb"), r("Muchas Burela", "Burela", "http://bit.ly/3olfSTb"),
            r("Merkal Calzados", "Lugo", "https://www.merkal.com/"), r("MERKAL CALZADOS", "Vigo", "http://merkal.com/"),
            r("Merkal Calzados S.L.", "Ourense"), r("Miss Tic", "Lugo"), r("Miss-tic Xinzo", "Xinzo de Limia"),
            r("Calzados", "Lugo"), r("Calzados", "Vigo")]
    tam = sorted(len(g) for g in agrupar_cadenas(rows))
    assert tam == [1, 1, 2, 2, 3], tam  # 'Calzados' genérico no une


def test_cliente_por_nombre_queda_ya_cliente_con_0_puntos():
    rows = [{"nombre": "Lolitamoda Noia", "municipio_origen": "Noia", "direccion": "z", "web": ""}]
    r = asyncio.run(analyze(rows, _args()))[0]
    assert r["prioridad"] == "Ya cliente" and r["puntuacion"] == 0 and r["prioridad_sin_cliente"]
