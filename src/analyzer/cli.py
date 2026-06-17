from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import logging
import time
from pathlib import Path
from typing import List, Optional

import openpyxl

from src.analyzer.brand_filter import is_excluded, load_brands
from src.analyzer.email_extract import get_email
from src.analyzer.fingerprint import detect_platform, fetch_page, is_social_url
from src.analyzer.scoring import (
    compute_score,
    coords_from_maps_url,
    count_stores_by_brand,
    load_avatares,
    load_eci_locations,
    load_weights,
    num_tiendas_for,
)
from src.comunidad.dataset import get_poblacion_municipio
from src.utils.logging import setup_logging

LOGGER = logging.getLogger(__name__)

# CSV field names (must match the scraper output)
_CSV_FIELDS = [
    "nombre", "telefono", "direccion", "web",
    "rating", "categoria", "source_query", "retrieved_at_utc", "maps_url",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Google Maps Scraper — Analyzer")
    parser.add_argument("--csv-path", required=True, help="Ruta al CSV de entrada")
    parser.add_argument(
        "--brands-path",
        default="config/excluded_brands.json",
        help="Ruta al JSON de marcas excluidas",
    )
    return parser


def _derive_xlsx_path(csv_path: str) -> str:
    return str(Path(csv_path).with_suffix(".xlsx"))


def _read_csv(csv_path: str) -> List[dict]:
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows


def _dedup_rows(rows: List[dict]) -> List[dict]:
    seen: set = set()
    out: List[dict] = []
    for row in rows:
        raw = "|".join([
            row.get("nombre", "").lower().strip(),
            row.get("direccion", "").lower().strip(),
            row.get("telefono", "").lower().strip(),
        ])
        key = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


async def _run(args: argparse.Namespace) -> None:
    start_ts = time.perf_counter()

    brands = load_brands(args.brands_path)
    LOGGER.info("Marcas excluidas cargadas: %d", len(brands))

    # Cargar configs de scoring (re-leídas en cada ejecución → editables sin reiniciar nada)
    weights = load_weights()
    avatares = load_avatares()
    eci_locations = load_eci_locations()
    LOGGER.info(
        "Scoring: %d criterios | %d avatares | %d centros ECI",
        len(weights["criterios"]), len(avatares), len(eci_locations),
    )

    rows = _read_csv(args.csv_path)
    rows = _dedup_rows(rows)
    LOGGER.info("Registros en CSV: %d", len(rows))

    # Pre-cómputo: contar tiendas por marca normalizada en TODO el dataset (incluye filtradas)
    brand_counts = count_stores_by_brand(rows, name_field="nombre")
    LOGGER.info("Marcas únicas detectadas: %d", len(brand_counts))

    xlsx_path = _derive_xlsx_path(args.csv_path)

    metrics = {"filtered": 0, "analyzed": 0, "stores": 0, "errors": 0}
    analysis_rows: List[dict] = []

    for row in rows:
        nombre = row.get("nombre", "")
        web = row.get("web", "").strip()

        if is_excluded(nombre, brands):
            metrics["filtered"] += 1
            LOGGER.info("[filtrado] %s", nombre)
            _emit_stats(metrics)
            continue

        # Normalizar URL: añadir https:// si falta protocolo
        if web and not web.startswith(("http://", "https://")):
            web = "https://" + web

        # Determine store status — guardamos el HTML para reusarlo en email_extract
        html: "Optional[str]" = None
        if not web or is_social_url(web):
            es_tienda = "-"
            tecnologia = "-"
        else:
            html = await fetch_page(web)
            if html is None:
                es_tienda = "-"
                tecnologia = "-"
                metrics["errors"] += 1
            else:
                is_store, platform = detect_platform(html)
                es_tienda = "Sí" if is_store else "No"
                tecnologia = platform if platform else "-"
                if is_store:
                    metrics["stores"] += 1

        # ── Cálculo de scoring ──────────────────────────────────────────
        # Coords: preferir columnas lat/lon (motor API las persiste); si no, del maps_url (scraper)
        lat, lon = None, None
        if (row.get("lat") or "").strip() and (row.get("lon") or "").strip():
            try:
                lat, lon = float(row["lat"]), float(row["lon"])
            except ValueError:
                lat, lon = None, None
        if lat is None:
            lat, lon = coords_from_maps_url(row.get("maps_url", ""))
        municipio = (row.get("municipio_origen") or "").strip()
        poblacion = get_poblacion_municipio(municipio) if municipio else None
        n_tiendas = num_tiendas_for(nombre, brand_counts, municipio)

        if es_tienda == "Sí":
            madurez = "ecommerce_funcional"
        elif web:
            # Hay web pero no se detectó e-commerce funcional (puede ser red social,
            # web informativa o web caída). Se trata como presencia básica.
            madurez = "solo_redes_sociales"
        else:
            madurez = "sin_presencia"

        ctx = {
            "lat": lat, "lon": lon,
            "poblacion": poblacion or 0,
            "num_tiendas": n_tiendas,
            "madurez": madurez,
            "tecnologia": tecnologia if tecnologia and tecnologia != "-" else None,
        }
        score = compute_score(ctx, eci_locations=eci_locations, avatares=avatares, weights=weights)

        # Email: real desde el HTML ya descargado, o ficticio basado en el nombre
        email = get_email(html, nombre)

        metrics["analyzed"] += 1
        analysis_rows.append({
            **row,
            "es_tienda": es_tienda,
            "tecnologia": tecnologia,
            "prioridad": score["prioridad"],
            "puntuacion": score["puntuacion_total"],
            "justificacion": score["justificacion"],
            "email": email,
        })

        LOGGER.info(
            "[analizado] %s | tienda=%s | %s | %dpts → %s | email=%s",
            nombre, es_tienda, tecnologia, score["puntuacion_total"],
            score["prioridad"], email,
        )
        _emit_stats(metrics)

    # Write XLSX
    wb = openpyxl.Workbook()

    # Sheet 1: original data
    ws1 = wb.active
    ws1.title = "Datos originales"
    if rows:
        headers = list(rows[0].keys())
        ws1.append(headers)
        for row in rows:
            ws1.append([row.get(h, "") for h in headers])

    # Sheet 2: analysis results
    ws2 = wb.create_sheet("Análisis")
    if analysis_rows:
        analysis_headers = list(analysis_rows[0].keys())
        ws2.append(analysis_headers)
        for row in analysis_rows:
            ws2.append([row.get(h, "") for h in analysis_headers])

    wb.save(xlsx_path)
    LOGGER.info("XLSX guardado: %s", xlsx_path)

    elapsed = time.perf_counter() - start_ts
    LOGGER.info(
        "── Resumen análisis ──\ntotal=%d filtrados=%d analizados=%d tiendas=%d errores=%d elapsed_s=%.2f",
        len(rows),
        metrics["filtered"],
        metrics["analyzed"],
        metrics["stores"],
        metrics["errors"],
        elapsed,
    )
    _emit_stats(metrics)


def _emit_stats(metrics: dict) -> None:
    LOGGER.info(
        "ASTATS filtered=%d analyzed=%d stores=%d errors=%d",
        metrics["filtered"], metrics["analyzed"], metrics["stores"], metrics["errors"],
    )


def main() -> None:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
