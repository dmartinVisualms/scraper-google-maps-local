from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import time
from typing import Any, Optional

from playwright.async_api import async_playwright

from src.browser.pool import ContextPool, PooledContext
from src.comunidad.dataset import get_poblacion_municipio
from src.geo.coords import coords_from_maps_url
from src.geo.grid import Sector, build_sector_grid, filter_by_polygon
from src.geo.nominatim import fetch_city_geodata
from src.geo.polygon import point_in_polygon, polygon_from_geojson
from src.pipeline.csv_writer import StreamingCsvWriter
from src.scraper.maps_detail import extract_business_record
from src.scraper.maps_search import SearchResultRef, collect_result_refs, open_maps_and_search
from src.utils.logging import setup_logging
from src.utils.retry import retry_async

LOGGER = logging.getLogger(__name__)


def parse_bool(value: str) -> bool:
    lower = value.lower().strip()
    if lower in {"1", "true", "yes", "y"}:
        return True
    if lower in {"0", "false", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"Booleano inválido: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Google Maps Scraper")
    parser.add_argument("--city", required=False, default=None)
    parser.add_argument("--category", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--headless", type=parse_bool, default=True)
    parser.add_argument("--max-results", type=int, default=0)
    parser.add_argument("--slow-ms", type=int, default=250)
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument("--concurrency", type=int, default=3,
                        help="Número de contextos Playwright simultáneos (default: 3)")
    parser.add_argument(
        "--zones", type=str, default=None,
        help="[Avanzado] JSON manual de zonas. Si no se indica, se genera automáticamente via Nominatim.",
    )
    parser.add_argument(
        "--adaptive-subdivision", type=parse_bool, default=True,
        dest="adaptive_subdivision",
        help="Activar subdivisión adaptativa de sectores (default: true)",
    )
    parser.add_argument(
        "--comunidad", type=str, default=None,
        help="Nombre de la comunidad autónoma (ej: 'Galicia'). Encadena el scraping por sus municipios. Mutuamente excluyente con --zones.",
    )
    parser.add_argument(
        "--min-poblacion", type=int, default=5000, dest="min_poblacion",
        help="Población mínima de los municipios a incluir (default: 5000). Sólo aplica con --comunidad.",
    )
    parser.add_argument(
        "--search-mode", choices=["auto", "text", "grid"], default="auto",
        dest="search_mode",
        help="Estrategia de búsqueda: 'auto' (según población), 'text' (sólo textual), "
             "'grid' (sólo grid fino). Default: auto.",
    )
    parser.add_argument(
        "--population", type=int, default=None,
        help="Población del --city (override). Si no se indica, se busca en el dataset; "
             "si tampoco, fallback a grid fino.",
    )
    return parser


def _select_strategy(population: Optional[int], mode: str) -> dict:
    """Devuelve {'text': bool, 'grid': Optional[dict]} según población y modo CLI.

    - mode='text': sólo búsqueda textual administrativa.
    - mode='grid': sólo grid fino (zoom 16, cell 0.003) — comportamiento previo.
    - mode='auto':
        * < 15k    → sólo texto
        * 15k–40k  → texto + grid suave (zoom 14, cell 0.01)
        * ≥ 40k    → grid fino (zoom 16, cell 0.003)
        * sin dato → grid fino (preserva comportamiento previo)
    """
    if mode == "text":
        return {"text": True, "grid": None}
    if mode == "grid":
        return {"text": False, "grid": {"zoom": 16, "cell_deg": 0.003}}
    # mode == "auto"
    p = population if population is not None else 999_999
    if p < 15_000:
        return {"text": True, "grid": None}
    if p < 40_000:
        return {"text": True, "grid": {"zoom": 14, "cell_deg": 0.01}}
    return {"text": False, "grid": {"zoom": 16, "cell_deg": 0.003}}


async def _process_refs(
    refs: list,
    page: Any,
    query: str,
    slow_ms: int,
    sector_label: str,
    sector: Optional[Sector],
    csv_writer: StreamingCsvWriter,
    metrics: dict,
    municipio_origen: str = "",
    municipio_polygon=None,
) -> None:
    """Procesa una lista de refs escribiendo cada registro al CSV inmediatamente.
    Actualiza metrics en tiempo real y emite STATS cada 10 registros.

    Filtrado geográfico:
    - Si `municipio_polygon` se proporciona, los registros fuera del polígono
      del municipio se descartan (preferido — preciso, sin falsos positivos
      por límites administrativos).
    - En su defecto, si `sector` se proporciona, se usa su bbox como antes.
    """
    local_processed = 0
    local_errors = 0
    bbox = sector.bbox() if sector is not None else None

    for ref in refs:
        # Cap global: parar tan pronto como el CSV alcance --max-results
        if csv_writer.is_full:
            LOGGER.info(
                "[%s] Cap global de %d alcanzado — saltando refs restantes",
                sector_label, csv_writer.total_written,
            )
            return
        url = ref.maps_url

        async def go_to_detail() -> None:
            await page.goto(url, wait_until="domcontentloaded")

        try:
            await retry_async(go_to_detail, attempts=2)
            await asyncio.sleep((slow_ms + random.randint(20, 150)) / 1000)
            record = await retry_async(
                lambda: extract_business_record(page, query),
                attempts=2,
            )
            if not record.nombre:
                local_errors += 1
                metrics["errors"] += 1
                LOGGER.warning("[%s] Sin nombre (omitido): %s", sector_label, url)
                continue

            # Filtrado geográfico: polígono (preferido) o bbox del sector (fallback)
            blat, blon = coords_from_maps_url(record.maps_url)
            if blat is not None:
                if municipio_polygon is not None:
                    if not point_in_polygon(municipio_polygon, blat, blon):
                        metrics["filtered_out_of_polygon"] += 1
                        LOGGER.debug(
                            "[%s] Filtrado fuera del polígono del municipio: %s (%.5f, %.5f)",
                            sector_label, record.nombre, blat, blon,
                        )
                        continue
                elif bbox is not None:
                    min_lat, max_lat, min_lon, max_lon = bbox
                    if not (min_lat <= blat <= max_lat and min_lon <= blon <= max_lon):
                        metrics["filtered_out_of_polygon"] += 1
                        LOGGER.debug(
                            "[%s] Filtrado fuera de bbox: %s (%.5f, %.5f)",
                            sector_label, record.nombre, blat, blon,
                        )
                        continue

            if municipio_origen:
                record.municipio_origen = municipio_origen

            await csv_writer.write_record(record)
            local_processed += 1
            metrics["processed"] += 1

            if local_processed % 10 == 0:
                LOGGER.info(
                    "[%s] Progreso: %d/%d procesados | %d válidos | %d errores",
                    sector_label, local_processed, len(refs), csv_writer.total_written, local_errors,
                )
                LOGGER.info(
                    "STATS discovered=%d processed=%d valid=%d errors=%d",
                    metrics["discovered"], metrics["processed"],
                    csv_writer.total_written, metrics["errors"],
                )
        except Exception as exc:  # noqa: BLE001
            local_errors += 1
            metrics["errors"] += 1
            LOGGER.warning("[%s] Error procesando %s: %s", sector_label, url, exc)


MIN_CELL_DEG = 0.002  # ~220 m — límite mínimo de subdivisión adaptativa


def _subdivide(sector: Sector) -> list:
    """Divide un sector en 4 sub-sectores (NW, NE, SW, SE) con la mitad del tamaño."""
    half = sector.cell_deg / 2
    quarter = half / 2
    new_zoom = min(sector.zoom + 1, 16)
    return [
        Sector(lat=sector.lat + quarter, lon=sector.lon - quarter, zoom=new_zoom, cell_deg=half),
        Sector(lat=sector.lat + quarter, lon=sector.lon + quarter, zoom=new_zoom, cell_deg=half),
        Sector(lat=sector.lat - quarter, lon=sector.lon - quarter, zoom=new_zoom, cell_deg=half),
        Sector(lat=sector.lat - quarter, lon=sector.lon + quarter, zoom=new_zoom, cell_deg=half),
    ]


async def _process_sector(
    label: str,
    sector: Sector,
    pool: ContextPool,
    query: str,
    csv_writer: StreamingCsvWriter,
    args: argparse.Namespace,
    metrics: dict,
    municipio_origen: str = "",
    municipio_polygon=None,
) -> None:
    """Procesa un sector geográfico: search → collect → extract → write CSV.

    La subdivisión adaptativa ocurre FUERA del bloque try/finally para que el
    contexto Playwright se libere al pool ANTES de lanzar los sub-sectores.
    Sin esto, con concurrency=N todos los slots quedan ocupados esperando
    sub-tareas que nunca pueden adquirir un slot → deadlock.
    """
    if csv_writer.is_full:
        LOGGER.debug("[%s] Cap global ya alcanzado, saltando sector", label)
        return
    pooled: PooledContext = await pool.acquire()
    needs_subdivision = False
    try:
        LOGGER.info(
            "── Sector %s @ %.5f, %.5f zoom=%d (cell=%.4f°) ──",
            label, sector.lat, sector.lon, sector.zoom, sector.cell_deg,
        )

        await retry_async(
            lambda: open_maps_and_search(
                pooled.page, query, lat=sector.lat, lon=sector.lon, zoom=sector.zoom
            ),
            attempts=3,
        )

        # max_results ya no se aplica a nivel de sector — actúa como cap global
        # del CSV, comprobado vía csv_writer.is_full antes de procesar cada ref.
        result = await collect_result_refs(
            page=pooled.page,
            slow_ms=args.slow_ms,
            max_results=0,
            polygon=municipio_polygon,
        )

        discovered = len(result.refs)
        metrics["discovered"] += discovered
        LOGGER.info("[%s] Descubiertos: %d (acumulado: %d)", label, discovered, metrics["discovered"])
        # Stats tempranas — la UI las muestra nada más terminar el discovery
        LOGGER.info(
            "STATS discovered=%d processed=%d valid=%d errors=%d",
            metrics["discovered"], metrics["processed"], csv_writer.total_written, metrics["errors"],
        )

        await _process_refs(
            refs=result.refs,
            page=pooled.page,
            query=query,
            slow_ms=args.slow_ms,
            sector_label=label,
            sector=sector,
            csv_writer=csv_writer,
            metrics=metrics,
            municipio_origen=municipio_origen,
            municipio_polygon=municipio_polygon,
        )

        LOGGER.info("[%s] Sector completado: %d válidos en CSV", label, csv_writer.total_written)
        # Stats finales del sector
        LOGGER.info(
            "STATS discovered=%d processed=%d valid=%d errors=%d",
            metrics["discovered"], metrics["processed"], csv_writer.total_written, metrics["errors"],
        )

        # No subdividir si paramos por densidad (Google ya está expandiendo fuera)
        needs_subdivision = (
            args.adaptive_subdivision
            and not result.reached_end
            and not result.density_stop
        )

    except Exception as exc:  # noqa: BLE001
        LOGGER.error("[%s] Sector falló: %s", label, exc)
        metrics["errors"] += 1
    finally:
        # Liberar el slot SIEMPRE antes de lanzar sub-tareas (evita deadlock)
        await pool.release(pooled)

    # ── Subdivisión adaptativa (fuera del try, contexto ya liberado) ───────
    if needs_subdivision:
        if sector.cell_deg > MIN_CELL_DEG:
            sub_sectors = _subdivide(sector)
            LOGGER.info(
                "[%s] ↳ Subdividiendo en %d (cell %.4f° → %.4f°)",
                label, len(sub_sectors), sector.cell_deg, sector.cell_deg / 2,
            )
            sub_tasks = [
                _process_sector(
                    f"{label}.{i + 1}", sub, pool, query, csv_writer, args, metrics,
                    municipio_origen=municipio_origen,
                    municipio_polygon=municipio_polygon,
                )
                for i, sub in enumerate(sub_sectors)
            ]
            await asyncio.gather(*sub_tasks)
        else:
            LOGGER.warning(
                "[%s] ⚠ Celda mínima (%.4f°) — puede haber resultados sin capturar",
                label, sector.cell_deg,
            )
            metrics["heuristic_stops"] += 1


async def _build_sectors_for_city(
    args: argparse.Namespace,
    city: str,
    grid_params: Optional[dict] = None,
    geodata=None,
) -> list:
    """Construye la lista de sectores para una ciudad.

    Si hay `--zones` manual, gana sobre todo lo demás. En caso contrario
    se construye un grid usando `grid_params={'zoom':..., 'cell_deg':...}`.
    `geodata` se puede pasar pre-cargado para no consultar Nominatim dos veces.
    """
    if args.zones:
        LOGGER.info("Modo manual: leyendo zonas desde --zones")
        try:
            zones_data = json.loads(args.zones)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--zones no es un JSON válido: {exc}") from exc
        sectors = [
            Sector(lat=z["lat"], lon=z["lon"], zoom=z.get("zoom", 14))
            for z in zones_data
        ]
        LOGGER.info("Zonas manuales: %d sectores", len(sectors))
        return sectors

    if grid_params is None:
        grid_params = {"zoom": 16, "cell_deg": 0.003}

    if geodata is None:
        LOGGER.info("Consultando Nominatim para: %s", city)
        geodata = await fetch_city_geodata(city)
    cell_deg = grid_params["cell_deg"]
    zoom = grid_params["zoom"]
    raw_sectors = build_sector_grid(geodata.bbox, cell_deg=cell_deg, zoom=zoom)
    sectors = filter_by_polygon(raw_sectors, geodata.polygon_geojson)
    LOGGER.info(
        "Grid generado: %d sectores (de %d en bbox) para %s [zoom=%d cell=%.4f°]",
        len(sectors), len(raw_sectors), geodata.display_name, zoom, cell_deg,
    )
    return sectors


async def _run_text_search(
    label: str,
    query: str,
    pool: ContextPool,
    csv_writer: StreamingCsvWriter,
    args: argparse.Namespace,
    metrics: dict,
    municipio_origen: str,
    municipio_polygon,
) -> None:
    """Ejecuta una única búsqueda textual administrativa (sin viewport URL)."""
    if csv_writer.is_full:
        return
    pooled: PooledContext = await pool.acquire()
    try:
        LOGGER.info("── Búsqueda textual %s — query='%s' ──", label, query)
        await retry_async(
            lambda: open_maps_and_search(pooled.page, query, text_only=True),
            attempts=3,
        )
        result = await collect_result_refs(
            page=pooled.page,
            slow_ms=args.slow_ms,
            max_results=0,
            polygon=municipio_polygon,
        )
        discovered = len(result.refs)
        metrics["discovered"] += discovered
        LOGGER.info("[%s] Descubiertos: %d (acumulado: %d)", label, discovered, metrics["discovered"])
        LOGGER.info(
            "STATS discovered=%d processed=%d valid=%d errors=%d",
            metrics["discovered"], metrics["processed"], csv_writer.total_written, metrics["errors"],
        )
        await _process_refs(
            refs=result.refs,
            page=pooled.page,
            query=query,
            slow_ms=args.slow_ms,
            sector_label=label,
            sector=None,
            csv_writer=csv_writer,
            metrics=metrics,
            municipio_origen=municipio_origen,
            municipio_polygon=municipio_polygon,
        )
        LOGGER.info("[%s] Búsqueda textual completada: %d válidos en CSV", label, csv_writer.total_written)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("[%s] Búsqueda textual falló: %s", label, exc)
        metrics["errors"] += 1
    finally:
        await pool.release(pooled)


async def _process_city_with_pool(
    args: argparse.Namespace,
    city: str,
    csv_writer: StreamingCsvWriter,
    pool: ContextPool,
    metrics: dict,
    municipio_origen: str = "",
    population: Optional[int] = None,
) -> int:
    """Procesa una sola ciudad usando el pool/CSV ya inicializados.

    Devuelve el incremento de registros válidos en el CSV durante esta ciudad.
    Selecciona estrategia híbrida (texto/grid/ambos) según `population` y
    `--search-mode`.
    """
    query = f"{args.category} en {city}"

    # Si --zones manual, comportamiento antiguo (bypass total)
    if args.zones:
        try:
            sectors = await _build_sectors_for_city(args, city)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("No se pudieron construir sectores para %s: %s", city, exc)
            return 0
        if not sectors:
            return 0
        before = csv_writer.total_written
        tasks = [
            _process_sector(
                f"{city}|{i + 1}/{len(sectors)}", s, pool, query, csv_writer, args, metrics,
                municipio_origen=municipio_origen,
            )
            for i, s in enumerate(sectors)
        ]
        await asyncio.gather(*tasks)
        return csv_writer.total_written - before

    # Resolver población: parámetro explícito o lookup en dataset
    if population is None:
        population = args.population
    if population is None:
        population = get_poblacion_municipio(municipio_origen or city.split(",")[0])

    strategy = _select_strategy(population, args.search_mode)
    LOGGER.info(
        "Estrategia para %s: pob=%s mode=%s → text=%s grid=%s",
        city, population, args.search_mode, strategy["text"], strategy["grid"],
    )

    # Cargar geodata una sola vez (polígono + bbox para el grid)
    municipio_polygon = None
    geodata = None
    try:
        geodata = await fetch_city_geodata(city)
        municipio_polygon = polygon_from_geojson(geodata.polygon_geojson)
        if municipio_polygon is None:
            LOGGER.info("Sin polígono Nominatim para %s — sin filtrado fino", city)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("No se pudo cargar geodata de %s: %s — sigo sin polígono", city, exc)

    before = csv_writer.total_written

    # Fase texto
    if strategy["text"]:
        await _run_text_search(
            label=f"{city}|text",
            query=query,
            pool=pool,
            csv_writer=csv_writer,
            args=args,
            metrics=metrics,
            municipio_origen=municipio_origen,
            municipio_polygon=municipio_polygon,
        )

    # Fase grid
    if strategy["grid"] is not None and geodata is not None and not csv_writer.is_full:
        try:
            sectors = await _build_sectors_for_city(
                args, city, grid_params=strategy["grid"], geodata=geodata,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("No se pudieron construir sectores para %s: %s", city, exc)
            sectors = []
        if sectors:
            LOGGER.info(
                "Procesando ciudad: %s | %d sectores | query='%s'",
                city, len(sectors), query,
            )
            tasks = [
                _process_sector(
                    f"{city}|{i + 1}/{len(sectors)}", s, pool, query, csv_writer, args, metrics,
                    municipio_origen=municipio_origen,
                    municipio_polygon=municipio_polygon,
                )
                for i, s in enumerate(sectors)
            ]
            await asyncio.gather(*tasks)
        else:
            LOGGER.warning("Sin sectores para %s en fase grid", city)
    elif strategy["grid"] is not None and geodata is None:
        LOGGER.warning(
            "Estrategia incluía grid pero no hay geodata — saltando fase grid para %s", city,
        )

    return csv_writer.total_written - before


async def _run(args: argparse.Namespace) -> None:
    # Validación: comunidad y zones son mutuamente excluyentes
    if args.comunidad and args.zones:
        raise ValueError("--comunidad y --zones son mutuamente excluyentes")
    if not args.comunidad and not args.city:
        raise ValueError("Debes indicar --city o --comunidad")

    start_ts = time.perf_counter()

    # CSV compartido entre todas las ciudades (dedup global automático).
    # max_records: cap global. 0 = sin límite.
    csv_writer = StreamingCsvWriter(args.output, max_records=args.max_results)
    LOGGER.info(
        "CSV: %s (cap=%s)",
        args.output, args.max_results if args.max_results > 0 else "sin límite",
    )

    metrics = {
        "discovered": 0, "processed": 0, "errors": 0,
        "heuristic_stops": 0, "filtered_out_of_polygon": 0,
    }

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=args.headless)
    pool = ContextPool(browser=browser, n=args.concurrency, timeout_ms=args.timeout_ms)

    try:
        if args.comunidad:
            from src.comunidad.runner import run_comunidad

            async def process_city_fn(city_str: str, municipio_origen: str, poblacion: int) -> int:
                return await _process_city_with_pool(
                    args, city_str, csv_writer, pool, metrics,
                    municipio_origen=municipio_origen, population=poblacion,
                )

            await run_comunidad(
                args.comunidad, args.min_poblacion, process_city_fn,
                is_full=lambda: csv_writer.is_full,
            )
        else:
            await _process_city_with_pool(args, args.city, csv_writer, pool, metrics)
    finally:
        await browser.close()
        await pw.stop()

    elapsed = time.perf_counter() - start_ts
    LOGGER.info("── Resumen final ──")
    LOGGER.info(
        "discover=%d processed=%d valid=%d duplicates=%d filtered_polygon=%d errors=%d elapsed_s=%.2f",
        metrics["discovered"],
        metrics["processed"],
        csv_writer.total_written,
        metrics["processed"] - csv_writer.total_written,
        metrics["filtered_out_of_polygon"],
        metrics["errors"],
        elapsed,
    )
    if metrics["filtered_out_of_polygon"] > 0:
        LOGGER.info(
            "↳ %d registro(s) filtrados por estar fuera del polígono del municipio",
            metrics["filtered_out_of_polygon"],
        )
    if metrics["heuristic_stops"] > 0:
        LOGGER.warning(
            "⚠ %d sector(es) se detuvieron por heurística — puede haber resultados fuera del viewport",
            metrics["heuristic_stops"],
        )
    else:
        LOGGER.info("✓ Todos los sectores confirmaron fin de resultados")


def main() -> None:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
