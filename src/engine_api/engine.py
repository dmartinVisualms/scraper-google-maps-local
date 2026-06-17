"""Motor API (Places API New Text Search) — alternativa al scraper Playwright.

Convergente con el scraper: produce BusinessRecord y escribe el mismo CSV.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

from src.engine_api import client as api_client
from src.engine_api.cost import estimate_cost
from src.engine_api.mapper import map_place
from src.engine_api.usage import record_and_report
from src.pipeline.export_csv import export_csv

LOGGER = logging.getLogger("src.engine_api.engine")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def metrics_path(output: str) -> str:
    return output[:-4] + "_metrics.json" if output.endswith(".csv") else output + "_metrics.json"


def _dedup(records: List) -> List:
    # API googleMapsUri es "?cid=..."; normalize_maps_url (split en "?") los colapsaría.
    # Dedup por URL completa; fallback a nombre|dir|tel cuando no hay URL.
    seen = set()
    out = []
    for r in records:
        key = r.maps_url.strip() or f"{r.nombre}|{r.direccion}|{r.telefono}".lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


async def run_api_engine(
    *,
    category: str,
    targets: List[Dict],
    output: str,
    included_type: Optional[str] = None,
    max_results: int = 0,
) -> Dict:
    """targets: [{"nombre": <etiqueta>, "location": <texto tras 'en'>}].
    Escribe el CSV y <output>_metrics.json; devuelve {metrics, quota, valid}.
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Define GOOGLE_MAPS_API_KEY en el entorno para el motor API.")

    retrieved_at = _utc_now_iso()
    started = time.perf_counter()
    pages = min(3, (max_results + 19) // 20) if max_results else 3

    records: List = []
    queries: List[str] = []
    total_requests = 0
    total_pages = 0

    async with aiohttp.ClientSession() as session:
        for t in targets:
            q = f"{category} en {t['location']}"
            queries.append(q)
            res = await api_client.search_text(
                q, api_key, session=session, max_pages=pages, included_type=included_type,
            )
            total_requests += res.requests
            total_pages += res.pages
            for place in res.places:
                records.append(map_place(
                    place, source_query=q, municipio_origen=t["nombre"], retrieved_at_utc=retrieved_at,
                ))
            LOGGER.info("[API][%s] %d resultados", t["nombre"], len(res.places))

    results_raw = len(records)
    deduped = _dedup(records)
    if max_results:
        deduped = deduped[:max_results]
    elapsed_s = round(time.perf_counter() - started, 2)

    export_csv(output, deduped)
    quota = record_and_report(total_requests, tier="enterprise")
    cost = estimate_cost(total_requests, tier="enterprise")

    # Línea parseable por el frontend (panel de cuota/coste)
    LOGGER.info(
        "APISTATS run_requests=%d month_requests=%d free_cap=%d remaining=%d within_free=%s est_cost_usd=%.2f",
        quota["run_requests"], quota["month_requests"], quota["free_cap"],
        quota["remaining"], str(quota["within_free"]).lower(), quota["est_cost_usd"],
    )

    metrics = {
        "engine": "places_api_new",
        "category": category,
        "queries": queries,
        "requests": total_requests,
        "pages": total_pages,
        "results_raw": results_raw,
        "results_deduped": len(deduped),
        "elapsed_s": elapsed_s,
        "tier": "enterprise",
        "run_est_cost_usd": cost.est_cost_usd,
        "quota": quota,
        "retrieved_at_utc": retrieved_at,
    }
    Path(metrics_path(output)).write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    LOGGER.info(
        "CSV: %s (%d válidos de %d crudos) | mes: %d/%d peticiones (%s cuota gratis) | coste mes est. $%.2f",
        output, len(deduped), results_raw, quota["month_requests"], quota["free_cap"],
        "dentro de" if quota["within_free"] else "FUERA de", quota["est_cost_usd"],
    )
    return {"metrics": metrics, "quota": quota, "valid": len(deduped)}
