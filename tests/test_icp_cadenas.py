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
