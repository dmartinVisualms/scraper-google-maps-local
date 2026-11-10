# Revisión manual 2026-11-09 (Opus): clasifica los negocios P1–P3 en listas de páginas personalizadas.
# Uso: python -m scripts.icp_listas_paginas (tras scripts.icp_unificar y scripts.icp_negocios). Las exclusiones por nombre son decisiones de esa revisión.
import json, re, sys
from collections import Counter
from pathlib import Path
from unidecode import unidecode
import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

OUT = Path(__file__).resolve().parents[1] / "out"
_ws = openpyxl.load_workbook(OUT / "icp_negocios.xlsx", read_only=True)["Negocios"]
_it = _ws.iter_rows(values_only=True)
_h = list(next(_it))
TODOS = [dict(zip(_h, r)) for r in _it]
# Candidatos: P1–P3, y los clientes que sin serlo serían P1–P3 (se quedan, marcados «Ya cliente» con 0 puntos).
c = [n for n in TODOS if n["prioridad"] in ("P1", "P2", "P3")
     or (n["prioridad"] == "Ya cliente" and n.get("prioridad_sin_cliente") in ("P1", "P2", "P3"))]
p = lambda s: unidecode((s or "").lower())

CADENAS = {"merkal calzados": "cadena (22+ locales)", "courir": "cadena internacional", "snipes": "cadena internacional",
           "deichmann": "cadena internacional", "new yorker": "cadena internacional", "forum sport vallsur": "cadena (Forum Sport)",
           "ovs": "cadena italiana de marca propia", "rks cc pontevella": "cadena (78 tiendas)", "charanga": "marca propia con cadena de tiendas",
           "r.c. celta": "tienda oficial de club (monomarca)", "intersport rubians": "franquicia Intersport", "intersport bouso": "franquicia Intersport"}
FUERA = {  # revisión manual: no es tienda de moda
    "justo munoz": "juguetería", "lafuga cycling": "ciclismo", "armeria jardin": "caza y pesca", "verticalia": "EPIs / trabajos verticales",
    "brico noia": "bricolaje", "mayper": "ropa de trabajo / EPIs", "mb4 vestimos": "ropa de trabajo", "vsgun": "airsoft",
    "babiek moto": "equipamiento de moto", "maprovi": "calzado de seguridad", "armeria mago": "caza y pesca", "helya patines": "patinaje y hockey",
    "moto bike pontevedra": "ciclismo", "rg bikes": "ciclismo", "bici o con": "ciclismo", "shop4fit": "decoración / hogar",
    "greengolf": "golf", "bap horse": "hípica", "decalupa": "juguetería", "andrade mar": "pesca", "hobbybike": "ciclismo",
    "momentos gourmet": "alimentación / puericultura", "tienda hipica": "hípica", "marino ropas de trabajo": "ropa de trabajo",
    "retro rocket vintage": "segunda mano (vintage)", "deportes jsc caza": "caza y pesca", "ballet and you": "material de danza",
    "xanela da danza": "material de danza", "pelouros": "puericultura (sillas, carritos)", "tatahuete box": "puericultura (sillas, carritos)",
    "ares baby": "puericultura", "chupete": "puericultura", "makingtennis": "tenis (material)", "babidu": "",
}
KW_FUERA = {"bici": "ciclismo", "bike": "ciclismo", "armeria": "caza y pesca", "pesca": "pesca", "golf": "golf", "hipica": "hípica",
            "jugueter": "juguetería", "ropa de trabajo": "ropa de trabajo", "laboral": "ropa de trabajo", "ortoped": "ortopedia",
            "airsoft": "airsoft", "moto ": "motos"}
DEBILES = {"it's only": "marcas detectadas = proveedores mayoristas", "4uatro estaciones": "proveedores mayoristas",
           "cero de costa": "marcas poco claras", "martinetta": "sin catálogo de marcas", "milpies": "sin catálogo de marcas",
           "almacenes prieto": "surtido mixto", "bebinos": "mucha puericultura además de moda infantil", "ulanka": "marca propia con red de tiendas (verificar)"}

# Páginas ABM publicadas (abm-tiendas/out-prod/<slug>.html ↔ tiendas/<slug>.json): se cruzan con cada negocio por dominio web.
ABM = Path(__file__).resolve().parents[2] / "abm-tiendas"
BASE_URL = "https://velfix.com/sectores/tiendas-ropa/"
dominios = lambda u: {re.sub(r"^(https?://)?(www\.)?", "", x.strip().lower()).split("/")[0] for x in (u or "").split(",") if x.strip()}  # noqa: E731
PAGINAS = {d: f"{BASE_URL}{f.stem}/" for f in sorted((ABM / "out-prod").glob("*.html"))
           for d in dominios(json.loads((ABM / "tiendas" / f"{f.stem}.json").read_text()).get("web"))}
url_pagina = lambda n: next((PAGINAS[d] for d in dominios(n.get("web")) if d in PAGINAS), None)  # noqa: E731


def match(d, nombre):
    n = p(nombre)
    return next((v for k, v in d.items() if k in n), None)

def contacto(n):
    return bool(n["email"] or n["instagram"])

filas = []
for n in c:
    nombre = n["negocio"]
    motivo = ""
    prio = n.get("prioridad_sin_cliente") or n["prioridad"] if n["prioridad"] == "Ya cliente" else n["prioridad"]
    if match(CADENAS, nombre) or (n["etiqueta"] or "").startswith("CADENA"):
        pag, motivo = "cadena (aparte)", match(CADENAS, nombre) or n["etiqueta"]
    elif match(FUERA, nombre) is not None or match(KW_FUERA, nombre + " " + (n["categorias"] or "")):
        pag, motivo = "fuera", (match(FUERA, nombre) or match(KW_FUERA, nombre + " " + (n["categorias"] or "")) or "fuera de sector")
    else:
        m = n["num_marcas"] or 0
        if prio == "P1" and m >= 5 and not match(DEBILES, nombre):
            pag = "P1"
        elif prio in ("P1", "P2") and m >= 5 and contacto(n) and not match(DEBILES, nombre):
            pag = "P2"
        else:
            pag = "P3"
        motivo = match(DEBILES, nombre) or ""
    ya = "Ya cliente" if n["prioridad"] == "Ya cliente" else ""
    filas.append({**n, "pagina": pag, "ya_cliente": ya, "nota_revision": motivo, "url_pagina": url_pagina(n)})
print(Counter(f["pagina"] for f in filas), Counter(f["pagina"] for f in filas if f["ya_cliente"]))

COLS = [("pagina", "Página"), ("ya_cliente", "Ya cliente"), ("negocio", "Negocio"), ("url_pagina", "Página personalizada (URL)"),
        ("municipios", "Municipios"),
        ("locales_csv", "Locales en Maps"), ("num_tiendas_web", "Tiendas según su web"), ("prioridad", "Prioridad ICP"),
        ("prioridad_sin_cliente", "Prioridad sin ser cliente"), ("puntuacion", "Puntos"), ("num_marcas", "Nº marcas"),
        ("marcas_detectadas", "Marcas que trabaja"), ("es_tienda_online", "Tienda online"), ("tecnologia", "Tecnología"),
        ("web", "Web"), ("instagram", "Instagram"), ("email", "Email"), ("telefono", "Teléfono"),
        ("nota_revision", "Nota de la revisión"), ("evidencias", "Evidencias"), ("nombres", "Nombres agrupados"),
        ("direcciones", "Direcciones")]
ANCHO = {"url_pagina": 52, "negocio": 30, "municipios": 30, "marcas_detectadas": 50, "evidencias": 60, "nombres": 40, "direcciones": 50,
         "web": 28, "instagram": 32, "email": 28, "nota_revision": 34}
wb = openpyxl.Workbook()
wb.remove(wb.active)
naranja = PatternFill("solid", fgColor="FFE6D5")


def hoja(nombre, rows, cols=COLS):
    ws = wb.create_sheet(nombre)
    ws.append([t for _, t in cols])
    for cell in ws[1]:
        cell.font, cell.fill = Font(bold=True), naranja
    for r in rows:
        ws.append([r.get(k) for k, _ in cols])
        for cell in ws[ws.max_row]:  # URL de la página personalizada: enlace clicable
            if isinstance(cell.value, str) and cell.value.startswith(BASE_URL):
                cell.hyperlink = cell.value
    ws.freeze_panes, ws.auto_filter.ref = "D2", ws.dimensions
    for i, (k, _) in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = ANCHO.get(k, 12)


orden = lambda r: (bool(r["ya_cliente"]), -(r["puntuacion"] or 0), -(r["num_marcas"] or 0))  # noqa: E731
crit = wb.create_sheet("Criterios")
cuenta = lambda f: (sum(1 for r in filas if f(r)), sum(1 for r in filas if f(r) and r["url_pagina"]))  # noqa: E731
por_pag = lambda t: cuenta(lambda r: r["pagina"] == t)  # noqa: E731
con_url_total = sum(1 for n in TODOS if url_pagina(n))
for line in [
    ["Listas de negocios para páginas personalizadas (ABM Velfix)"], [],
    ["Pestaña", "Criterio", "Negocios", "Con página personalizada"],
    ["Origen", "out/icp_unificado.xlsx agrupado en negocios (scripts/icp_negocios.py): mismo negocio si comparte web, Instagram, teléfono, email de su dominio, o el mismo nombre sin municipio en la misma comunidad. Pestaña «Todos los negocios».", len(TODOS), con_url_total],
    ["Página P1", "Prioridad ICP P1, catálogo propio con 5 o más marcas de terceros, 1–7 locales.", *por_pag("P1")],
    ["Página P2", "Prioridad P1/P2 con 5 o más marcas en su web y un contacto real (email del dominio o Instagram).", *por_pag("P2")],
    ["Página P3", "El resto de multimarca: P3, pocas marcas detectadas, o multimarca deducido de su descripción/Instagram.", *por_pag("P3")],
    ["Ya cliente", "Web hecha con Velfix (Meigasoft) o en config/clientes_velfix.json. Siguen en su lista, al final, con 0 puntos: interés nulo para esta campaña. (Ya contados en sus pestañas.)", *cuenta(lambda r: r["ya_cliente"])],
    ["Cadenas (aparte)", "Cadenas grandes, franquicias de grupo (Intersport) o marca propia con tiendas: otra campaña.", *por_pag("cadena (aparte)")],
    ["Fuera de sector", "Revisión manual: ciclismo, caza y pesca, golf, hípica, ropa de trabajo/EPIs, juguetería, puericultura, airsoft…", *por_pag("fuera")],
    ["Ojo", "La lista P3 no se ha revisado una a una. «Página personalizada» = URL publicada en velfix.com/sectores/tiendas-ropa/ (abm-tiendas/out-prod), cruzada por dominio web."]]:
    crit.append(line)
crit["A1"].font = Font(bold=True, size=14)
for cell in crit[3]:
    cell.font, cell.fill = Font(bold=True), naranja
crit.column_dimensions["A"].width, crit.column_dimensions["B"].width = 20, 120
crit.column_dimensions["C"].width = crit.column_dimensions["D"].width = 24
for t in ("P1", "P2", "P3"):
    hoja(f"Páginas {t}", sorted([f for f in filas if f["pagina"] == t], key=orden))
hoja("Cadenas (aparte)", [f for f in filas if f["pagina"] == "cadena (aparte)"])
hoja("Fuera de sector", [f for f in filas if f["pagina"] == "fuera"])
ws = wb.create_sheet("Todos los negocios")
ws.append(_h + ["Página personalizada (URL)"])
for n in TODOS:
    ws.append([n.get(k) for k in _h] + [url_pagina(n)])
    if ws.cell(ws.max_row, len(_h) + 1).value:
        ws.cell(ws.max_row, len(_h) + 1).hyperlink = ws.cell(ws.max_row, len(_h) + 1).value
ws.freeze_panes = "B2"
wb.save(OUT / "icp_paginas_personalizadas.xlsx")
print(OUT / "icp_paginas_personalizadas.xlsx")
