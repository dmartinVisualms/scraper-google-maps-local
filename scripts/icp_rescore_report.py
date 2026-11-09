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
import re
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import openpyxl

from src.analyzer.cli import _dedup_rows, _read_csv, analyze, write_xlsx
from src.analyzer.scoring import _GENERIC_BRAND_WORDS, _brand_key
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


_SIN_DOMINIO = {"instagram.com", "facebook.com", "bit.ly", "linktr.ee", "google.com", "wa.me", "goo.gl"}


def _dominio(web: str) -> str:
    host = urlparse(web if "//" in web else f"http://{web}").hostname or ""
    dom = ".".join(host.split(".")[-2:])
    if not dom or dom in _SIN_DOMINIO:
        return web.split("?")[0].lower()  # acortadores y redes: la URL entera
    return dom


def agrupar_cadenas(rows: list) -> list:
    """Une locales de la misma cadena: mismo dominio web, o mismo nombre sin municipio,
    o un nombre que empieza por el de otra cadena ('Muchas Noia' → 'Muchas').
    ponytail: union-find de 10 líneas; los nombres genéricos ('calzados') no unen nada."""
    padre = list(range(len(rows)))

    def raiz(i):
        while padre[i] != i:
            padre[i] = padre[padre[i]]
            i = padre[i]
        return i

    def unir(i, j):
        padre[raiz(i)] = raiz(j)

    primero = {}
    claves = [_brand_key(r["nombre"], r.get("municipio_origen") or "") for r in rows]
    for i, r in enumerate(rows):
        ks = [f"w:{_dominio(r['web'])}"] if (r.get("web") or "").strip() else []
        if claves[i] and not set(claves[i].split()) <= _GENERIC_BRAND_WORDS:
            ks.append(f"n:{claves[i]}")
        for k in ks:
            unir(i, primero.setdefault(k, i))
    for i, k in enumerate(claves):  # prefijos: 'muchas noia' con 'muchas'
        toks = k.split()
        for n in range(1, len(toks)):
            j = primero.get("n:" + " ".join(toks[:n]))
            if j is not None:
                unir(i, j)
                break
    grupos = {}
    for i, r in enumerate(rows):
        grupos.setdefault(raiz(i), []).append(r)
    return sorted(grupos.values(), key=len, reverse=True)


def _md_cell(s) -> str:
    return str(s if s is not None else "").replace("|", "/").replace("\n", " ")[:160]


async def main() -> None:
    args = argparse.Namespace(brands_path=str(ROOT / "config" / "excluded_brands.json"),
                              refresh=False, no_llm=False, dataforseo_max_usd=None,
                              ig_solo_cache=True)  # Instagram: solo lo ya pagado
    creds = _dataforseo_creds()
    budget = DataForSeoBudget(default_max_usd()) if creds else None
    ICP_OUT.mkdir(exist_ok=True)

    resumen, salen, nuevos_p1, cadenas = [], [], [], []
    fuentes = Counter()
    for name in FILES:
        rows = _dedup_rows(_read_csv(str(MAIN_OUT / f"{name}.csv")))
        old = _old_priorities(MAIN_OUT / f"{name}.xlsx")
        # Búsqueda de web en Google solo para los que eran P1/P2 en la versión normal (decisión 2026-11-09).
        p12 = {k for k, v in old.items() if v[0] in ("P1", "P2")}
        args.buscar_web = lambda row, p12=p12: _key(row) in p12
        res = await analyze(rows, args, ig_budget=budget)
        fuentes.update(r.get("web_fuente") or "sin web" for r in res)
        write_xlsx(str(ICP_OUT / f"{name}_icp.xlsx"), rows, res)

        prio = Counter(str(r["prioridad"]) for r in res)
        tipos = Counter(r["tipo_negocio"] for r in res)
        p1_antes = sum(1 for v in old.values() if v[0] == "P1")
        n_cad = sum(1 for r in res if r.get("etiqueta"))
        resumen.append((name, len(rows), p1_antes, prio.get("P1", 0), tipos.get("multimarca", 0),
                        prio.get("descartado", 0), prio.get("revision", 0), n_cad, tipos))
        cadenas += [r for r in res if r.get("etiqueta")]
        for r in res:
            antes = old.get(_key(r), (None, None))
            if antes[0] == "P1" and r["prioridad"] != "P1":
                salen.append((name, r, antes))
            if r["prioridad"] == "P1" and antes[0] != "P1":
                nuevos_p1.append((name, r, antes))

    grupos = agrupar_cadenas(cadenas)
    random.seed(42)
    muestra = random.sample(nuevos_p1, min(10, len(nuevos_p1)))
    gasto = budget.spent if budget else 0.0
    pendientes = budget.pending if budget else 0

    L = [f"# Informe: nueva puntuación con el analizador ICP Multimarca",
         "", f"Fecha: {date.today().isoformat()} · Versión ICP 0.9.10.x · DataForSEO gastado en esta ejecución: ${gasto:.2f}"
         + (f" (quedaron {pendientes} negocios sin web sin buscar por el límite de $1)" if pendientes else ""),
         "", "«Antes» es la prioridad del XLSX que ya existía junto a cada CSV, calculada con la versión normal.",
         "", "## Resumen por fichero", "",
         "| Fichero | Negocios | P1 antes | P1 ahora | Multimarca | Descartados | Revisión | Etiqueta CADENA |",
         "|---|---|---|---|---|---|---|---|"]
    for name, n, a, b, mm, desc, rev, cad, _ in resumen:
        L.append(f"| {name} | {n} | {a} | {b} | {mm} | {desc} | {rev} | {cad} |")
    tot = [sum(x[i] for x in resumen) for i in range(1, 8)]
    L.append("| **Total** | " + " | ".join(f"**{t}**" for t in tot) + " |")

    tipos_tot = Counter()
    for *_, t in resumen:  # noqa: B007
        tipos_tot.update(t)
    L += ["", "## De dónde sale la web analizada", "",
          "«otro local»: web de otro local de la misma cadena. «google»: búsqueda «nombre municipio» "
          "(solo para los que eran P1/P2 en la versión normal).", "", "| Origen | Negocios |", "|---|---|"]
    L += [f"| {f} | {n} |" for f, n in fuentes.most_common()]
    L += ["", "## Tipos de negocio (todos los ficheros)", "", "| Tipo | Negocios |", "|---|---|"]
    L += [f"| {t} | {n} |" for t, n in tipos_tot.most_common()]

    L += ["", f"## Cadenas (etiqueta CADENA): {len(grupos)} cadenas, {len(cadenas)} locales", "",
          "Negocios con 8 o más tiendas (en su web o en el CSV) o en config/cadenas.json. Nunca pasan de P2, "
          "pero pueden ser objetivo. Se agrupan por dominio web y por nombre sin municipio "
          "(«Muchas Noia» y «Muchas Burela» van juntas).", "",
          "| Cadena | Etiqueta | Locales en los CSV | Tipo | Prioridad | Web |", "|---|---|---|---|---|---|"]
    for g in grupos:
        tipos = Counter(r["tipo_negocio"] for r in g)
        prios = Counter(str(r["prioridad"]) for r in g)
        nombre = Counter(r["nombre"].strip() for r in g).most_common(1)[0][0]
        etiqueta = max((r["etiqueta"] for r in g), key=lambda e: int(re.sub(r"\D", "", e) or 0))
        web = next((r.get("web") for r in g if r.get("web")), "")
        L.append(f"| {_md_cell(nombre)} | {etiqueta} | {len(g)} | "
                 f"{', '.join(t for t, _ in tipos.most_common(2))} | "
                 f"{', '.join(f'{p} ({n})' for p, n in prios.most_common(3))} | {_md_cell(web)} |")

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
