"""Recupera la web de negocios sin web legible (Cadena Q: Maps no trae web o trae un dominio que da 403).

- `browser_fetcher()`: reintento con Chromium (Playwright) para webs que bloquean aiohttp.
- `buscar_web_google()`: busca «nombre municipio» en Google vía DataForSEO ($0,002) y elige
  el primer resultado cuyo dominio se parece al nombre (`elegir_web`).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
from typing import AsyncIterator, Optional, Tuple
from urllib.parse import urlparse

from unidecode import unidecode

from src.analyzer.icp.fetch import USER_AGENT, Fetcher

LOGGER = logging.getLogger(__name__)
_DATAFORSEO_URL = "https://api.dataforseo.com/v3/serp/google/organic/live/regular"

# Directorios, redes y marketplaces: nunca son la web propia del negocio.
_NO_PROPIAS = (
    "instagram.", "facebook.", "tiktok.", "twitter.", "x.com", "youtube.", "linkedin.", "pinterest.",
    "google.", "paginasamarillas.", "cylex", "tripadvisor.", "yelp.", "infoisinfo.", "einforma.",
    "axesor.", "empresite.", "guiaempresas.", "infoempresa.", "habitissimo.", "wikipedia.", "amazon.",
    "ebay.", "wallapop.", "vinted.", "milanuncios.", "zalando.", "elcorteingles.", "glovo", "justeat",
    "doctoralia.", "groupon.", "booking.", "mapcarta.", "waze.", "apple.com", "foursquare.",
    "tuugo.", "cybo.", "hotfrog.", "yably.", "infobel.", "abre.", "horarios", "cerca", "dondecomprar",
    "ofertia.", "tiendeo.", "salir.com", "11870.", "qdq.", "vulka.", "eltiempo.", "lavozdegalicia.",
    "farodevigo.", "elcorreogallego.", "diariodeleon.", "elnortedecastilla.", "lne.es", "elcomercio.es",
)
_GENERICAS = {"tienda", "tiendas", "moda", "modas", "boutique", "calzados", "calzado", "zapateria",
              "ropa", "deportes", "sport", "sports", "outlet", "kids", "infantil", "shop", "store",
              "the", "la", "el", "los", "las", "de", "del", "y", "en", "sl", "s", "l", "sa"}


def _plano(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unidecode(s or "").lower())


def dominio_parece_nombre(dominio: str, nombre: str, municipio: str = "") -> bool:
    """'cadenaq.es' ~ 'Cadena Q'; 'thebesmoda.com' ~ 'Thebes'. Ignora palabras genéricas y el municipio."""
    raiz = _plano(dominio.split(".")[-2] if dominio.count(".") >= 1 else dominio)
    muni = set(_plano(w) for w in (municipio or "").split())
    toks = [_plano(w) for w in re.split(r"[\s\-_/&,.()]+", nombre or "")]
    toks = [t for t in toks if t and t not in _GENERICAS and t not in muni]
    if not toks or not raiz:
        return False
    # ponytail: todas las palabras del nombre dentro del dominio ('lacadenadn' no vale para 'Cadena Q')
    return "".join(toks) in raiz or all(t in raiz for t in toks)


def elegir_web(items: list, nombre: str, municipio: str = "") -> Optional[str]:
    for it in items:
        if it.get("type") != "organic":
            continue
        # DataForSEO devuelve la URL envuelta en google.com/goto: el dominio real va en breadcrumb.
        url = it.get("url") or ""
        if "google.com/goto" in url or not url:
            url = (it.get("breadcrumb") or "").split(" ")[0]
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if not host or any(b in host for b in _NO_PROPIAS):
            continue
        if dominio_parece_nombre(host, nombre, municipio):
            return f"https://{host}/"
    return None


async def buscar_web_google(nombre: str, municipio: str, creds: tuple,
                            timeout_s: int = 30) -> Tuple[Optional[str], float]:
    """Devuelve (web_o_None, coste_usd). Coste 0 si la llamada falló (y entonces no se cachea)."""
    import aiohttp

    query = f"{nombre} {municipio}".strip()
    token = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
    payload = [{"keyword": query[:500], "location_code": 2724, "language_code": "es", "depth": 10}]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as session:
            async with session.post(_DATAFORSEO_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    LOGGER.warning("DataForSEO HTTP %d para '%s'", resp.status, query)
                    return None, 0.0
                data = await resp.json()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("DataForSEO error para '%s': %s", query, exc)
        return None, 0.0
    task = (data.get("tasks") or [{}])[0]
    cost = float(task.get("cost") or 0.0)
    items = ((task.get("result") or [{}])[0] or {}).get("items") or []
    return elegir_web(items, nombre, municipio), cost


@contextlib.asynccontextmanager
async def browser_fetcher(concurrency: int = 4) -> AsyncIterator[Fetcher]:
    """Fetcher con Chromium real: pasa los 403 de webs que bloquean clientes HTTP simples."""
    from playwright.async_api import async_playwright

    sem = asyncio.Semaphore(concurrency)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=USER_AGENT, locale="es-ES", ignore_https_errors=True)

        async def fetch(url: str):
            async with sem:
                page = await ctx.new_page()
                try:
                    resp = await page.goto(url, timeout=20000, wait_until="domcontentloaded")
                    if resp is None or resp.status >= 400:
                        return page.url, None
                    ctype = (resp.headers or {}).get("content-type", "")
                    if "json" in ctype:
                        return page.url, await resp.text()
                    return page.url, await page.content()
                except Exception:  # noqa: BLE001
                    return url, None
                finally:
                    await page.close()

        try:
            yield fetch
        finally:
            await browser.close()


if __name__ == "__main__":
    assert dominio_parece_nombre("cadenaq.es", "Cadena Q", "Benavente")
    assert dominio_parece_nombre("thebesmoda.com", "Thebes", "Vigo")
    assert not dominio_parece_nombre("modaspepa.com", "Modas Lola", "Vigo")
    assert not dominio_parece_nombre("lacadenadn.com", "Cadena Q", "Palencia")
    assert elegir_web([{"type": "organic", "url": "https://google.com/goto?url=x",
                        "breadcrumb": "https://www.cadenaq.es › tiendas"}], "Cadena Q") == "https://cadenaq.es/"
    assert elegir_web([{"type": "organic", "url": "https://www.paginasamarillas.es/x"},
                       {"type": "organic", "url": "https://cadenaq.es/tiendas"}], "Cadena Q") == "https://cadenaq.es/"
    print("ok")
