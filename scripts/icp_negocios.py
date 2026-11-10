"""Agrupa las filas de out/icp_unificado.xlsx en negocios (mismo negocio con otro nombre o localidad).

Uso: python -m scripts.icp_negocios  → out/icp_negocios.xlsx (hoja «Negocios», una fila por negocio)

Une dos filas si comparten: web propia (host), Instagram, teléfono, email real o dirección;
o el mismo nombre sin municipio dentro de la misma comunidad (más débil: se marca en «unido_por»).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import openpyxl
from unidecode import unidecode

from src.analyzer.scoring import _GENERIC_BRAND_WORDS, _brand_key

OUT = Path(__file__).resolve().parents[1] / "out"
SRC, DEST = OUT / "icp_unificado.xlsx", OUT / "icp_negocios.xlsx"
_PRIO = {"Ya cliente": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "revision": 5, "descartado": 6}
_HOSTS_COMPARTIDOS = ("instagram.", "facebook.", "bit.ly", "linktr.ee", "google.", "wa.me", "goo.gl", "tiktok.")


def _host(web) -> str:
    if not web:
        return ""
    h = (urlparse(web if "//" in web else f"http://{web}").hostname or "").lower().removeprefix("www.")
    return "" if any(s in h for s in _HOSTS_COMPARTIDOS) else h


def _ig(url) -> str:
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", url or "")
    return m.group(1).lower().strip(".") if m else ""


def _tel(t) -> str:
    d = re.sub(r"\D", "", str(t or ""))[-9:]
    return d if len(d) == 9 else ""


_CORREO_PERSONAL = ("gmail.", "hotmail.", "yahoo.", "outlook.", "icloud.", "live.", "telefonica.net", "msn.")


def _email(e, web_host: str = "") -> str:
    """Email real. Si no es un correo personal, su dominio debe ser el de la web del negocio:
    así no une el email de la agencia que hizo la web (testing@meigasoft.es en 54 webs)."""
    e = (e or "").strip().lower()
    if not e or e.startswith("vmarketing@") or "@" not in e:
        return ""
    dom = e.split("@", 1)[1]
    if any(p in dom for p in _CORREO_PERSONAL):
        return e
    return e if web_host and (dom in web_host or web_host.split(".")[-2] in dom) else ""


def _dir(d) -> str:
    return re.sub(r"[^a-z0-9]", "", unidecode((d or "").lower()))


def claves(r: dict) -> list:
    region = r["fichero"].split("_tiendas")[0].split("_zapaterias")[0]
    host = _host(r.get("web_analizada") or r.get("web"))
    k = [("web", host), ("instagram", _ig(r.get("instagram"))),
         ("telefono", _tel(r.get("telefono"))), ("email", _email(r.get("email"), host))]
    nk = _brand_key(r.get("nombre") or "", r.get("municipio_origen") or "")
    if nk and not set(nk.split()) <= _GENERIC_BRAND_WORDS and len(nk.replace(" ", "")) >= 4:
        k.append(("nombre", f"{region}|{nk}"))
    return [(t, v) for t, v in k if v]


def agrupar(rows: list) -> list:
    padre = list(range(len(rows)))

    def raiz(i):
        while padre[i] != i:
            padre[i] = padre[padre[i]]
            i = padre[i]
        return i

    primero, motivo = {}, defaultdict(set)
    for i, r in enumerate(rows):
        for t, v in claves(r):
            j = primero.setdefault((t, v), i)
            if j != i and raiz(i) != raiz(j):
                motivo[i].add(t)
                padre[raiz(i)] = raiz(j)
    grupos = defaultdict(list)
    for i in range(len(rows)):
        grupos[raiz(i)].append(i)
    return [([rows[i] for i in idx], set().union(*(motivo[i] for i in idx))) for idx in grupos.values()]


def resumen(g: list, unido_por: set) -> dict:
    mejor = min(g, key=lambda r: (_PRIO.get(r["prioridad"], 9), _PRIO.get(r.get("prioridad_sin_cliente"), 9),
                                  -(r.get("puntuacion") or 0)))
    locales = {(_dir(r.get("direccion")) or r["nombre"]) for r in g}
    uniq = lambda xs: ", ".join(dict.fromkeys(x for x in xs if x))  # noqa: E731
    return {
        "negocio": Counter(r["nombre"].strip() for r in g).most_common(1)[0][0],
        "nombres": uniq(r["nombre"].strip() for r in g),
        "locales_csv": len(locales),
        "municipios": uniq(r.get("municipio_origen") for r in g),
        "prioridad": mejor["prioridad"], "puntuacion": mejor.get("puntuacion"),
        "prioridad_sin_cliente": mejor.get("prioridad_sin_cliente") or "",
        "tipo_negocio": mejor["tipo_negocio"], "etiqueta": mejor.get("etiqueta") or "",
        "num_marcas": mejor.get("num_marcas"), "marcas_detectadas": mejor.get("marcas_detectadas"),
        "num_tiendas_web": mejor.get("num_tiendas_web"), "es_tienda_online": mejor.get("es_tienda"),
        "tecnologia": mejor.get("tecnologia"),
        "web": uniq(_host(r.get("web_analizada") or r.get("web")) for r in g),
        "instagram": uniq(r.get("instagram") for r in g),
        "email": uniq(_email(r.get("email"), _host(r.get("web_analizada") or r.get("web"))) for r in g),
        "telefono": uniq(r.get("telefono") for r in g),
        "direcciones": " | ".join(dict.fromkeys(r.get("direccion") or "" for r in g)),
        "categorias": uniq(r.get("categoria") for r in g),
        "ficheros": uniq(r["fichero"].rsplit("_", 2)[0] for r in g),
        "evidencias": mejor.get("evidencias"), "justificacion": mejor.get("justificacion"),
        "unido_por": ", ".join(sorted(unido_por)),
        "filas": len(g),
    }


def main() -> list:
    ws = openpyxl.load_workbook(SRC, read_only=True)["Análisis"]
    it = ws.iter_rows(values_only=True)
    h = list(next(it))
    rows = [dict(zip(h, r)) for r in it]
    negocios = sorted((resumen(g, m) for g, m in agrupar(rows)),
                      key=lambda n: (_PRIO.get(n["prioridad"], 9), -(n["puntuacion"] or 0)))
    wb = openpyxl.Workbook(write_only=True)
    out = wb.create_sheet("Negocios")
    out.append(list(negocios[0].keys()))
    for n in negocios:
        out.append(list(n.values()))
    wb.save(DEST)
    print(f"{DEST}: {len(rows)} filas → {len(negocios)} negocios")
    return negocios


if __name__ == "__main__":
    main()
