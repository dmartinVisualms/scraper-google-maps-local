# Revisión manual 2026-11-09 (Opus): clasifica los negocios P1–P3 en listas de páginas personalizadas.
# Uso: python scripts/icp_listas_paginas.py <carpeta con candidatos.json>. Las exclusiones por nombre son decisiones de esa revisión.
import json, re, sys
from collections import Counter
from unidecode import unidecode
import openpyxl
S = sys.argv[1]
c = json.load(open(f"{S}/candidatos.json"))
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

def match(d, nombre):
    n = p(nombre)
    return next((v for k, v in d.items() if k in n), None)

def contacto(n):
    return bool(n["email"] or n["instagram"])

filas = []
for n in c:
    nombre = n["negocio"]
    motivo = ""
    if "lolitamoda" in (n["web"] or "") or "lolita" in p(nombre):
        pag, motivo = "fuera", "ya es cliente de Velfix (Lolitamoda)"
    elif match(CADENAS, nombre) or (n["etiqueta"] or "").startswith("CADENA"):
        pag, motivo = "cadena (aparte)", match(CADENAS, nombre) or n["etiqueta"]
    elif match(FUERA, nombre) is not None or match(KW_FUERA, nombre + " " + (n["categorias"] or "")):
        pag, motivo = "fuera", (match(FUERA, nombre) or match(KW_FUERA, nombre + " " + (n["categorias"] or "")) or "fuera de sector")
    else:
        m = n["num_marcas"] or 0
        if n["prioridad"] == "P1" and m >= 5 and not match(DEBILES, nombre):
            pag = "P1"
        elif n["prioridad"] in ("P1", "P2") and m >= 5 and contacto(n) and not match(DEBILES, nombre):
            pag = "P2"
        else:
            pag = "P3"
        motivo = match(DEBILES, nombre) or ""
    filas.append({**n, "pagina": pag, "nota_revision": motivo})
json.dump(filas, open(f"{S}/listas.json", "w"), ensure_ascii=False, default=str)
print(Counter(f["pagina"] for f in filas))
for f in filas:
    if f["pagina"] == "P1": print("P1", f["negocio"], "|", f["municipios"][:40], "|", f["num_marcas"], f["puntuacion"])
