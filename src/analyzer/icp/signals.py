"""Extracción de señales de la web de un negocio para clasificar su tipo.

Visita la portada y, si existen, como mucho estas páginas: listado de marcas,
`/products.json` (Shopify), página de tiendas y "sobre nosotros".
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

from src.analyzer.icp.fetch import Fetcher

_A_RE = re.compile(r'<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_STRIP_RE = re.compile(r"<(script|style|noscript|svg)\b.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_DESC_RE = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', re.I)
# Código postal español: 01000–52999, aislado de otros dígitos.
_CP_RE = re.compile(r"(?<![\d.,/-])((?:0[1-9]|[1-4]\d|5[0-2])\d{3})(?![\d.,/-])")
_JSONLD_BRAND_RE = re.compile(r'"brand"\s*:\s*(?:\{[^{}]*?"name"\s*:\s*"([^"]+)"|"([^"]+)")')

_ASSET_RE = re.compile(r"(wp-content|wp-includes|\.(css|js|png|jpe?g|gif|svg|webp|pdf|xml)$)", re.I)
# Enlace a una marca concreta: /brand/116-selected, /marcas/armani, /marca/salsa/ …
_BRAND_LINK_RE = re.compile(
    r"/(?:brand|brands|marca|marcas|manufacturer|fabricante|fabricantes|product-brand|product_brand|pa_marca|marques)"
    r"/(?:\d+-)?([a-z0-9][a-z0-9-]{1,60})/?$", re.I)
_BRAND_QUERY_RE = re.compile(r"[?&](?:filter_marca|filter_brand|product_brand|pa_marca)=([^&]+)", re.I)
_BRAND_LIST_RE = re.compile(r"/(?:brands|marcas|marques|fabricantes|manufacturer|nuestras-marcas)/?$", re.I)
_STORES_RE = re.compile(r"(tiendas|stores|store-locator|localizador|puntos-de-venta|donde-estamos|encuentranos)", re.I)
_ABOUT_RE = re.compile(r"(sobre-nosotros|quienes-somos|about|nuestra-historia|conocenos|la-empresa)", re.I)

_FRANQUICIA = ["abre tu franquicia", "franquicias", "franquicia", "hazte franquiciado", "franchising"]
_B2B = ["b2b", "distribuidores", "mayoristas", "venta al por mayor", "area profesional", "área profesional"]
_PROPIA_FUERTE = [
    "diseñamos", "diseño propio", "diseños propios", "made in spain", "hecho en españa", "fabricamos",
    "fabricación propia", "taller propio", "marca propia", "colección propia", "nuestros diseños", "diseñado en",
]
_PROPIA_DEBIL = ["nueva colección", "nuestra colección", "colecciones"]

_SECTOR = {
    "moda": ["ropa", "moda", "vestido", "camisa", "pantal", "chaqueta", "abrigo", "zapato", "calzado",
             "zapatilla", "bota", "sandalia", "bolso", "camiseta", "falda", "vaquero", "jeans", "jersey",
             "sudadera", "lencería", "deportiv"],
    "perfumeria": ["perfum", "fragancia", "eau de", "colonia"],
    "cosmetica": ["cosmét", "maquillaje", "skincare", "labial", "crema facial", "sérum", "manicura"],
    "joyeria": ["joyer", "relojer", "oro de ley", "plata de ley", "quilates"],
    "optica": ["óptica", "gafas graduadas", "lentillas", "audiología"],
    "hogar": ["decoración", "menaje", "textil hogar", "sábanas", "edredón"],
}
_SUBSECTOR = {
    "calzado": ["zapat", "calzado", "botas", "sandalia", "sneaker", "zapatilla", "mocasín"],
    "deporte": ["deport", "running", "fitness", "trekking", "pádel", "padel", "ciclismo", "montaña"],
    "infantil": ["niño", "niña", "bebé", "infantil", "kids", "junior"],
    "ropa": ["ropa", "vestido", "camisa", "pantal", "chaqueta", "abrigo", "camiseta", "falda", "jersey", "sudadera"],
}
_PUBLICO = {
    "mujer": ["mujer", "women", "señora"],
    "hombre": ["hombre", " men", "caballero"],
    "niño": ["niño", "niña", "kids", "bebé", "infantil"],
}


# Prefijo del código postal → provincia.
PROVINCIAS = {
    "01": "Álava", "02": "Albacete", "03": "Alicante", "04": "Almería", "05": "Ávila", "06": "Badajoz",
    "07": "Baleares", "08": "Barcelona", "09": "Burgos", "10": "Cáceres", "11": "Cádiz", "12": "Castellón",
    "13": "Ciudad Real", "14": "Córdoba", "15": "A Coruña", "16": "Cuenca", "17": "Girona", "18": "Granada",
    "19": "Guadalajara", "20": "Gipuzkoa", "21": "Huelva", "22": "Huesca", "23": "Jaén", "24": "León",
    "25": "Lleida", "26": "La Rioja", "27": "Lugo", "28": "Madrid", "29": "Málaga", "30": "Murcia",
    "31": "Navarra", "32": "Ourense", "33": "Asturias", "34": "Palencia", "35": "Las Palmas",
    "36": "Pontevedra", "37": "Salamanca", "38": "Santa Cruz de Tenerife", "39": "Cantabria",
    "40": "Segovia", "41": "Sevilla", "42": "Soria", "43": "Tarragona", "44": "Teruel", "45": "Toledo",
    "46": "Valencia", "47": "Valladolid", "48": "Bizkaia", "49": "Zamora", "50": "Zaragoza",
    "51": "Ceuta", "52": "Melilla",
}


@dataclass
class Signals:
    web: str
    final_url: str = ""
    ok: bool = False
    title: str = ""
    marcas: List[str] = field(default_factory=list)
    marcas_url: str = ""
    franquicia: List[str] = field(default_factory=list)
    b2b: List[str] = field(default_factory=list)
    propia_fuerte: List[str] = field(default_factory=list)
    propia_debil: List[str] = field(default_factory=list)
    num_tiendas_web: Optional[int] = None
    provincias: List[str] = field(default_factory=list)
    tiendas_url: str = ""
    sector_scores: Dict[str, int] = field(default_factory=dict)
    sector_real: str = ""
    subsector: str = ""
    publico: List[str] = field(default_factory=list)
    texto: str = ""  # resumen para el paso con LLM

    def to_dict(self) -> dict:
        return asdict(self)


def visible_text(html: str) -> str:
    text = _TAG_RE.sub(" ", _STRIP_RE.sub(" ", html or ""))
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _links(html: str, base: str) -> List[Tuple[str, str, str]]:
    """(url_absoluta, ruta, texto_del_enlace) de los enlaces internos."""
    host = urlparse(base).netloc.replace("www.", "")
    out, seen = [], set()
    for href, inner in _A_RE.findall(html or ""):
        url = urljoin(base, href.strip())
        p = urlparse(url)
        if p.scheme not in ("http", "https") or p.netloc.replace("www.", "") != host:
            continue
        if _ASSET_RE.search(p.path) or url in seen:
            continue
        seen.add(url)
        out.append((url, p.path, visible_text(inner)[:80]))
    return out


def _slug_to_name(slug: str) -> str:
    return unquote(slug).replace("-", " ").replace("_", " ").strip().title()


def _brands_from_links(links) -> set:
    names = set()
    for url, path, _ in links:
        m = _BRAND_LINK_RE.search(path)
        if m:
            names.add(_slug_to_name(m.group(1)))
        q = _BRAND_QUERY_RE.search(url)
        if q:
            names.add(_slug_to_name(q.group(1)))
    return names


def _pick(links, rx) -> Optional[str]:
    cands = [(len(path), url) for url, path, text in links if rx.search(path)]
    return min(cands)[1] if cands else None


def _count_terms(text: str, groups: Dict[str, List[str]]) -> Dict[str, int]:
    return {k: sum(text.count(t) for t in terms) for k, terms in groups.items()}


def _found(text: str, terms: List[str]) -> List[str]:
    return [t for t in terms if t in text]


async def extract_signals(web: str, fetch: Fetcher) -> Tuple[Signals, Optional[str]]:
    """Devuelve (señales, html_de_la_portada). El HTML se reutiliza en el analizador
    para la plataforma de e-commerce, el email y el Instagram."""
    final, html = await fetch(web)
    sig = Signals(web=web, final_url=final)
    if not html:
        return sig, None
    sig.ok = True
    m = _TITLE_RE.search(html)
    sig.title = visible_text(m.group(1))[:120] if m else ""
    links = _links(html, final)

    brands = _brands_from_links(links)
    for a, b in _JSONLD_BRAND_RE.findall(html):
        brands.add((a or b).strip())

    list_url = _pick(links, _BRAND_LIST_RE)
    if list_url:
        _, list_html = await fetch(list_url)
        if list_html:
            brands |= _brands_from_links(_links(list_html, list_url))
            sig.marcas_url = list_url

    if "cdn.shopify.com" in html:
        p = urlparse(final)
        pj_url = f"{p.scheme}://{p.netloc}/products.json?limit=250"
        _, pj = await fetch(pj_url)
        try:
            vendors = {pr.get("vendor", "").strip() for pr in json.loads(pj or "{}").get("products", [])}
            brands |= {v for v in vendors if v}
            sig.marcas_url = sig.marcas_url or pj_url
        except ValueError:
            pass

    sig.marcas = sorted(b for b in brands if 1 < len(b) <= 60)
    if sig.marcas and not sig.marcas_url:
        sig.marcas_url = final

    stores_url = _pick(links, _STORES_RE)
    if stores_url:
        _, st_html = await fetch(stores_url)
        if st_html:
            cps = sorted(set(_CP_RE.findall(visible_text(st_html))))
            if cps:
                sig.num_tiendas_web = len(cps)
                sig.provincias = sorted({c[:2] for c in cps})
                sig.tiendas_url = stores_url

    about_text = ""
    about_url = _pick(links, _ABOUT_RE)
    if about_url:
        _, ab_html = await fetch(about_url)
        about_text = visible_text(ab_html or "")[:2000]

    home_text = visible_text(html)
    link_text = " ".join(f"{path} {text}" for _, path, text in links)
    low = f"{home_text} {about_text} {link_text}".lower()
    # Las rutas usan guiones: "abre-tu-franquicia" también cuenta.
    low_paths = low.replace("-", " ")

    sig.franquicia = _found(low_paths, _FRANQUICIA)
    sig.b2b = _found(low_paths, _B2B)
    sig.propia_fuerte = _found(low, _PROPIA_FUERTE)
    sig.propia_debil = _found(low, _PROPIA_DEBIL)

    sig.sector_scores = _count_terms(low, _SECTOR)
    no_moda = {k: v for k, v in sig.sector_scores.items() if k != "moda"}
    top = max(no_moda, key=no_moda.get)
    sig.sector_real = top if no_moda[top] > sig.sector_scores["moda"] and no_moda[top] >= 8 else "moda"

    sub = _count_terms(low, _SUBSECTOR)
    ranked = sorted(sub.items(), key=lambda kv: -kv[1])
    if ranked[0][1] == 0:
        sig.subsector = ""
    elif ranked[1][1] >= 0.6 * ranked[0][1]:
        sig.subsector = "mixto"
    else:
        sig.subsector = ranked[0][0]
    pub = _count_terms(low, _PUBLICO)
    sig.publico = [k for k, v in pub.items() if v >= 3]

    meta = _META_DESC_RE.search(html)
    nav = sorted({text for _, _, text in links if 2 < len(text) < 40})[:60]
    parts = [
        f"Título: {sig.title}",
        f"Descripción: {meta.group(1)[:300]}" if meta else "",
        f"Menú: {', '.join(nav)}",
        f"Marcas del catálogo ({len(sig.marcas)}): {', '.join(sig.marcas[:60])}" if sig.marcas else "Marcas del catálogo: ninguna detectada",
        f"Tiendas en su web: {sig.num_tiendas_web} en provincias {', '.join(sig.provincias)}" if sig.num_tiendas_web else "",
        f"Sobre nosotros: {about_text[:1200]}" if about_text else "",
    ]
    sig.texto = "\n".join(p for p in parts if p)[:3500]
    return sig, html
