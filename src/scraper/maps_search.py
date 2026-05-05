from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

from playwright.async_api import Page

from src.geo.coords import coords_from_maps_url
from src.geo.polygon import point_in_polygon


LOGGER = logging.getLogger(__name__)


@dataclass
class SearchResultRef:
    name: str
    maps_url: str


@dataclass
class SearchResults:
    refs: list[SearchResultRef]
    reached_end: bool  # True = Google Maps confirmó fin de lista; False = parada por heurística
    density_stop: bool = False  # True = parada porque <5% de los nuevos refs caen en el polígono


RESULT_LINK_SELECTOR = 'a.hfpxzc'
SCROLL_CONTAINER_SELECTORS = [
    'div[role="feed"]',
    'div[aria-label*="Resultados"]',
    'div[aria-label*="Results"]',
]
SEARCH_INPUT_SELECTORS = [
    'input#searchboxinput',
    'input[aria-label="Buscar en Google Maps"]',
    'input[aria-label="Search Google Maps"]',
    'input[name="q"]',
]
CONSENT_BUTTONS = [
    'button:has-text("Rechazar todo")',
    'button:has-text("Aceptar todo")',
    'button:has-text("Accept all")',
    'button:has-text("Reject all")',
]
# Fragmentos invariantes del mensaje "fin de lista" en distintos idiomas.
# Sin "Has"/"You've" iniciales ni puntuación final → robusto frente a variantes
# (con/sin punto, mayúsculas, prefijos cambiantes de Google).
END_OF_LIST_TEXTS = [
    "llegado al final de la lista",   # ES
    "reached the end of the list",    # EN
]


async def _maybe_handle_consent(page: Page) -> None:
    for sel in CONSENT_BUTTONS:
        btn = page.locator(sel)
        if await btn.count() > 0:
            try:
                await btn.first.click(timeout=2000)
                await asyncio.sleep(0.8)
                return
            except Exception:  # noqa: BLE001
                continue


async def _find_search_input(page: Page):
    for selector in SEARCH_INPUT_SELECTORS:
        loc = page.locator(selector)
        if await loc.count() > 0:
            try:
                await loc.first.wait_for(state="visible", timeout=5000)
                return loc.first
            except Exception:  # noqa: BLE001
                continue
    return None


async def _has_reached_end(page: Page) -> bool:
    """Comprueba si Google Maps ha mostrado el mensaje de fin de lista.

    Usa `:has-text` (substring) en lugar de `text=` (match exacto) para tolerar
    variantes con/sin puntuación final, prefijos como "Has" / "You've", etc.
    """
    for text in END_OF_LIST_TEXTS:
        try:
            loc = page.locator(f':text-matches("{text}", "i")')
            if await loc.count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def open_maps_and_search(
    page: Page,
    query: str,
    lat: float | None = None,
    lon: float | None = None,
    zoom: int | None = None,
    text_only: bool = False,
) -> None:
    """Abre Google Maps y ejecuta la búsqueda.

    Si `text_only=True` (o no se pasan coords) se va a la home de Maps sin
    forzar un viewport, dejando que Google trate la query como búsqueda
    administrativa ("zapaterías en Vedra"). Útil para municipios pequeños
    donde el viewport-bias expande la búsqueda a localidades vecinas.
    """
    if not text_only and lat is not None and lon is not None and zoom is not None:
        viewport_url = f"https://www.google.com/maps/@{lat},{lon},{zoom}z"
        LOGGER.info("Viewport: %.5f, %.5f zoom=%s", lat, lon, zoom)
        await page.goto(viewport_url, wait_until="domcontentloaded")
    else:
        if text_only:
            LOGGER.info("Modo textual: búsqueda administrativa sin viewport")
        await page.goto("https://www.google.com/maps", wait_until="domcontentloaded")

    await _maybe_handle_consent(page)

    search_input = await _find_search_input(page)
    if search_input is None:
        await page.screenshot(path="out/error_search_input.png", full_page=True)
        content = await page.content()
        with open("out/error_search_input.html", "w", encoding="utf-8") as handle:
            handle.write(content)
        raise RuntimeError("No se encontró el input de búsqueda en Google Maps")

    await search_input.fill(query)
    await search_input.press("Enter")
    await asyncio.sleep(2)


async def _get_scroll_container(page: Page):
    for selector in SCROLL_CONTAINER_SELECTORS:
        try:
            await page.wait_for_selector(selector, timeout=10000)
            loc = page.locator(selector)
            if await loc.count() > 0:
                return loc.first
        except Exception:  # noqa: BLE001
            continue
    return None


async def collect_result_refs(
    page: Page,
    slow_ms: int,
    max_results: int,
    no_growth_limit: int = 12,
    polygon=None,
    density_threshold: float = 0.05,
    density_window: int = 3,
) -> SearchResults:
    """Recolecta refs de la lista de resultados.

    Si `polygon` se proporciona, se calcula el ratio de refs nuevos cuyas
    coordenadas caen dentro del polígono. Tras `density_window` batches
    consecutivos con ratio < `density_threshold` se aborta el scroll
    (Google está expandiendo la búsqueda a zonas externas).
    """
    seen: dict[str, SearchResultRef] = {}
    no_growth = 0
    reached_end = False
    density_stop = False
    recent_ratios: list[float] = []  # ratios de batches con refs nuevos

    container = await _get_scroll_container(page)
    if container is None:
        LOGGER.warning("No se encontró contenedor de resultados")
        return SearchResults(refs=[], reached_end=False)

    while True:
        links = page.locator(RESULT_LINK_SELECTOR)
        count = await links.count()

        before = len(seen)
        new_in_batch = 0
        new_in_polygon = 0
        for idx in range(count):
            link = links.nth(idx)
            href = (await link.get_attribute("href")) or ""
            href = href.strip()
            if not href:
                continue
            name = clean_name(await link.get_attribute("aria-label") or "")
            if href not in seen:
                seen[href] = SearchResultRef(name=name, maps_url=href)
                new_in_batch += 1
                if polygon is not None:
                    blat, blon = coords_from_maps_url(href)
                    if blat is not None and point_in_polygon(polygon, blat, blon):
                        new_in_polygon += 1
                new_total = len(seen)
                if new_total % 10 == 0:
                    LOGGER.info("Descubriendo resultados: %s encontrados…", new_total)
                if max_results > 0 and new_total >= max_results:
                    LOGGER.info("Se alcanzó max-results=%s", max_results)
                    return SearchResults(
                        refs=list(seen.values()), reached_end=False, density_stop=False,
                    )

        if len(seen) == before:
            no_growth += 1
        else:
            no_growth = 0

        # Early stop por densidad: si el polígono está activo y los últimos
        # `density_window` batches con refs nuevos cayeron casi todos fuera, parar.
        if polygon is not None and new_in_batch > 0:
            ratio = new_in_polygon / new_in_batch
            recent_ratios.append(ratio)
            if len(recent_ratios) > density_window:
                recent_ratios = recent_ratios[-density_window:]
            if (
                len(recent_ratios) == density_window
                and all(r < density_threshold for r in recent_ratios)
            ):
                density_stop = True
                LOGGER.info(
                    "✓ Parada por densidad: últimos %d batches con <%.0f%% en polígono "
                    "(Google expandió fuera del municipio)",
                    density_window, density_threshold * 100,
                )
                break

        # Comprobar fin de lista real de Google Maps
        if await _has_reached_end(page):
            reached_end = True
            LOGGER.info(
                "✓ Fin de resultados confirmado por Google Maps (%s encontrados)", len(seen)
            )
            break

        if no_growth >= no_growth_limit:
            LOGGER.warning(
                "⚠ Parada por heurística (%s iteraciones sin crecimiento) — "
                "puede haber más resultados fuera del viewport actual",
                no_growth_limit,
            )
            break

        await container.evaluate("el => el.scrollBy(0, el.clientHeight)")
        await asyncio.sleep((slow_ms + random.randint(20, 180)) / 1000)

    return SearchResults(
        refs=list(seen.values()), reached_end=reached_end, density_stop=density_stop,
    )


def clean_name(value: str) -> str:
    return " ".join(value.split()).strip()
