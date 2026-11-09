"""Vuelve a puntuar los CSV de la versión normal con el analizador ICP y escribe un informe.

Uso: python -m scripts.icp_rescore_report
- Lee los CSV de ../scraper-google-maps-local/out/ (no los copia).
- "Antes" = prioridad de la pestaña Análisis del XLSX que ya existe junto a cada CSV.
- Escribe un XLSX ICP por fichero en out/ de esta versión y el informe en abm-tiendas/.
- DataForSEO: un único límite de $1 para toda la ejecución.
"""
from __future__ import annotations

import argparse
import asyncio
import random
from collections import Counter
from datetime import date
from pathlib import Path

import openpyxl

from src.analyzer.cli import _dedup_rows, _read_csv, analyze, write_xlsx
from src.analyzer.instagram import DataForSeoBudget, _dataforseo_creds, default_max_usd
from src.utils.logging import setup_logging

ROOT = Path(__file__).resolve().parents[1]
MAIN_OUT = ROOT.parent / "scraper-google-maps-local" / "out"
ICP_OUT = ROOT / "out"
REPORT = ROOT.parent / "abm-tiendas" / "informe-reescoring-icp.md"
FILES = [
    "galicia_tiendas_de_ropa_20260630_111153",
    "galicia_zapaterias_20260617_144753",
    "galicia_tiendas_de_deportes_20260630_112813",
    "galicia_tiendas_de_ninos_20260630_113928",
    "principado_de_asturias_tiendas_de_ropa_20261104_124922",
    "castilla_y_leon_tiendas_de_ropa_20261105_125743",
]


def _key(r: dict) -> tuple:
    return ((r.get("nombre") or "").strip().lower(), (r.get("direccion") or "").strip().lower())


def _old_priorities(xlsx: Path) -> dict:
    if not xlsx.exists():
        return {}
    ws = openpyxl.load_workbook(xlsx, read_only=True)["Análisis"]
    it = ws.iter_rows(values_only=True)
    h = list(next(it))
    if "prioridad" not in h:
        return {}
    d = {}
    for r in it:
        row = dict(zip(h, r))
        d[_key(row)] = (row.get("prioridad"), row.get("puntuacion"))
    return d


def _md_cell(s) -> str:
    return str(s if s is not None else "").replace("|", "/").replace("\n", " ")[:160]


async def main() -> None:
    args = argparse.Namespace(brands_path=str(ROOT / "config" / "excluded_brands.json"),
                              refresh=False, no_llm=False, dataforseo_max_usd=None)
    creds = _dataforseo_creds()
    budget = DataForSeoBudget(default_max_usd()) if creds else None
    ICP_OUT.mkdir(exist_ok=True)

    resumen, salen, nuevos_p1 = [], [], []
    for name in FILES:
        rows = _dedup_rows(_read_csv(str(MAIN_OUT / f"{name}.csv")))
        old = _old_priorities(MAIN_OUT / f"{name}.xlsx")
        res = await analyze(rows, args, ig_budget=budget)
        write_xlsx(str(ICP_OUT / f"{name}_icp.xlsx"), rows, res)

        prio = Counter(str(r["prioridad"]) for r in res)
        tipos = Counter(r["tipo_negocio"] for r in res)
        p1_antes = sum(1 for v in old.values() if v[0] == "P1")
        resumen.append((name, len(rows), p1_antes, prio.get("P1", 0), tipos.get("multimarca", 0),
                        prio.get("descartado", 0), prio.get("revision", 0), tipos))
        for r in res:
            antes = old.get(_key(r), (None, None))
            if antes[0] == "P1" and r["prioridad"] != "P1":
                salen.append((name, r, antes))
            if r["prioridad"] == "P1" and antes[0] != "P1":
                nuevos_p1.append((name, r, antes))

    random.seed(42)
    muestra = random.sample(nuevos_p1, min(10, len(nuevos_p1)))
    gasto = budget.spent if budget else 0.0
    pendientes = budget.pending if budget else 0

    L = [f"# Informe: nueva puntuación con el analizador ICP Multimarca",
         "", f"Fecha: {date.today().isoformat()} · Versión ICP 0.9.10.x · DataForSEO gastado: ${gasto:.2f}"
         + (f" (quedaron {pendientes} negocios sin web sin buscar por el límite de $1)" if pendientes else ""),
         "", "«Antes» es la prioridad del XLSX que ya existía junto a cada CSV, calculada con la versión normal.",
         "", "## Resumen por fichero", "",
         "| Fichero | Negocios | P1 antes | P1 ahora | Multimarca | Descartados | Revisión |",
         "|---|---|---|---|---|---|---|"]
    for name, n, a, b, mm, desc, rev, _ in resumen:
        L.append(f"| {name} | {n} | {a} | {b} | {mm} | {desc} | {rev} |")
    tot = [sum(x[i] for x in resumen) for i in range(1, 7)]
    L.append(f"| **Total** | **{tot[0]}** | **{tot[1]}** | **{tot[2]}** | **{tot[3]}** | **{tot[4]}** | **{tot[5]}** |")

    tipos_tot = Counter()
    for *_, t in resumen:
        tipos_tot.update(t)
    L += ["", "## Tipos de negocio (todos los ficheros)", "", "| Tipo | Negocios |", "|---|---|"]
    L += [f"| {t} | {n} |" for t, n in tipos_tot.most_common()]

    L += ["", f"## Los que salen de P1 ({len(salen)})", "",
          "| Negocio | Municipio | Fichero | Antes | Ahora | Tipo | Evidencia principal |",
          "|---|---|---|---|---|---|---|"]
    for name, r, antes in sorted(salen, key=lambda x: (x[1]["tipo_negocio"], x[1]["nombre"])):
        ev = (r.get("evidencias") or "").split("; ")[0]
        L.append(f"| {_md_cell(r['nombre'])} | {_md_cell(r.get('municipio_origen'))} | {name.split('_20')[0]} | "
                 f"P1 ({antes[1]}) | {r['prioridad']} {r.get('puntuacion') or ''} | {r['tipo_negocio']} | {_md_cell(ev)} |")

    L += ["", f"## 10 nuevos P1 al azar para revisar a mano (de {len(nuevos_p1)} nuevos)", "",
          "| Negocio | Municipio | Web | Puntos | Evidencia | Antes |", "|---|---|---|---|---|---|"]
    for name, r, antes in muestra:
        L.append(f"| {_md_cell(r['nombre'])} | {_md_cell(r.get('municipio_origen'))} | {_md_cell(r.get('web'))} | "
                 f"{r.get('puntuacion')} | {_md_cell(r.get('evidencias'))} | {antes[0] or 'no estaba'} |")

    L += ["", "## Ficheros", "", f"XLSX con la pestaña Análisis ICP de cada fichero: `{ICP_OUT}/<fichero>_icp.xlsx`."]
    REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"Informe: {REPORT}")


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())
