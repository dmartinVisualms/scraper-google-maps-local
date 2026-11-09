"""Analizador ICP Multimarca.

Fases:
  A. Descarga en paralelo las webs (una vez por dominio) y extrae señales.
  B. Clasifica el tipo de negocio con reglas. Para negocios sin web, toma los
     fragmentos de Instagram de Google vía DataForSEO (con límite de gasto).
  C. Los dudosos pasan por Claude Haiku (claude -p, plan Team), en lotes.
  D. Puerta: solo los multimarca se puntúan; el resto queda 'descartado' o 'revision'.
Las señales y las respuestas del LLM se guardan en cache/icp/ para no repetirlas.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import openpyxl

from src.analyzer.brand_filter import is_excluded, load_brands
from src.analyzer.email_extract import extract_email_from_html, fictitious_email
from src.analyzer.fingerprint import detect_platform, is_social_url
from src.analyzer.icp import llm
from src.analyzer.icp.fetch import USER_AGENT, make_live_fetcher
from src.analyzer.icp.rules import UMBRAL_LLM, Clasificacion, classify, load_marcas_config, norm
from src.analyzer.icp.signals import PROVINCIAS, Signals, extract_signals
from src.analyzer.instagram import (
    DataForSeoBudget, SearchCache, _dataforseo_creds, default_max_usd,
    extract_instagram_from_html, resolve_instagram_with_snippets,
)
from src.analyzer.scoring import (
    compute_score, coords_from_maps_url, count_stores_by_brand, load_avatares,
    load_eci_locations, load_weights, num_tiendas_for,
)
from src.comunidad.dataset import get_poblacion_municipio
from src.utils.logging import setup_logging

LOGGER = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_CACHE_DIR = _ROOT / "cache" / "icp"
_CLIENTES = _ROOT / "config" / "clientes_velfix.json"

# Columnas del CSV que se omiten en la pestaña "Análisis" (siguen en "Datos originales").
_ANALYSIS_DROP = {"rating", "source_query", "retrieved_at_utc", "maps_url", "lat", "lon"}

_TIPO_LABEL = {
    "multimarca": "Multimarca", "marca_propia": "Marca propia",
    "marca_propia_franquicia": "Marca propia con franquicias",
    "franquiciado_monomarca": "Franquiciado monomarca", "no_moda": "No moda",
    "dudoso": "Dudoso", "cliente_velfix": "Cliente de Velfix",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Google Maps Scraper — Analizador ICP Multimarca")
    parser.add_argument("--csv-path", required=True, help="Ruta al CSV de entrada")
    parser.add_argument("--xlsx-path", default=None, help="Ruta del XLSX de salida (por defecto, junto al CSV)")
    parser.add_argument(
        "--dataforseo-max-usd", type=float, default=None,
        help="Gasto máximo en DataForSEO por ejecución (USD). Default: DATAFORSEO_MAX_USD_PER_RUN o 1.0",
    )
    parser.add_argument("--brands-path", default="config/excluded_brands.json")
    parser.add_argument("--refresh", action="store_true", help="Ignorar la caché de webs y del LLM")
    parser.add_argument("--no-llm", action="store_true", help="No usar el paso con LLM (los dudosos quedan en revisión)")
    return parser


def _read_csv(csv_path: str) -> List[dict]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _dedup_rows(rows: List[dict]) -> List[dict]:
    seen: set = set()
    out: List[dict] = []
    for row in rows:
        raw = "|".join(row.get(k, "").lower().strip() for k in ("nombre", "direccion", "telefono"))
        key = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _norm_web(web: str) -> str:
    web = (web or "").strip()
    if web and not web.startswith(("http://", "https://")):
        web = "https://" + web
    return web


def _host(web: str) -> str:
    return urlparse(web).netloc.lower().replace("www.", "")


class JsonCache:
    """Caché JSON en disco. ponytail: un fichero por tipo, reescrito al guardar;
    pasar a SQLite si crece por encima de unas decenas de MB."""

    def __init__(self, path: Path, enabled: bool = True) -> None:
        self.path, self.enabled = path, enabled
        try:
            self.data: Dict[str, dict] = json.loads(path.read_text(encoding="utf-8")) if enabled else {}
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str) -> Optional[dict]:
        return self.data.get(key) if self.enabled else None

    def put(self, key: str, value: dict) -> None:
        self.data[key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


async def _fase_webs(hosts: Dict[str, str], cache: JsonCache, concurrency: int = 8) -> Dict[str, dict]:
    """Para cada dominio: señales + datos de la portada (plataforma, email, Instagram)."""
    import aiohttp

    out: Dict[str, dict] = {}
    pending = {h: w for h, w in hosts.items() if cache.get(h) is None}
    for h in hosts:
        if h not in pending:
            out[h] = cache.get(h)
    LOGGER.info("Webs: %d dominios (%d en caché, %d por descargar)", len(hosts), len(out), len(pending))
    if not pending:
        return out

    sem = asyncio.Semaphore(concurrency)
    done = 0
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        fetch = make_live_fetcher(session)

        async def one(host: str, web: str) -> None:
            nonlocal done
            async with sem:
                sig, html = await extract_signals(web, fetch)
            is_store, platform = detect_platform(html) if html else (False, None)
            entry = {
                "signals": sig.to_dict(),
                "es_tienda": ("Sí" if is_store else "No") if html else "-",
                "tecnologia": (platform or "-") if html else "-",
                "email": extract_email_from_html(html),
                "instagram": extract_instagram_from_html(html) or "",
                "ts": int(time.time()),
            }
            out[host] = entry
            cache.put(host, entry)
            done += 1
            if done % 25 == 0 or done == len(pending):
                LOGGER.info("Webs analizadas: %d/%d", done, len(pending))
                cache.save()

        await asyncio.gather(*(one(h, w) for h, w in pending.items()))
    cache.save()
    return out


def _signals_from(entry: Optional[dict]) -> Optional[Signals]:
    if not entry:
        return None
    return Signals(**entry["signals"])


def _clientes() -> List[str]:
    try:
        return [norm(c).replace(" ", "") for c in json.loads(_CLIENTES.read_text(encoding="utf-8"))["clientes"]]
    except (OSError, ValueError, KeyError):
        return []


async def analyze(rows: List[dict], args: argparse.Namespace,
                  ig_budget: Optional[DataForSeoBudget] = None) -> List[dict]:
    """Devuelve las filas de la pestaña Análisis (incluye descartados y en revisión).
    `ig_budget` permite compartir el límite de DataForSEO entre varios ficheros."""
    brands = load_brands(args.brands_path)
    clientes = _clientes()
    marcas_cfg = load_marcas_config()
    tipicas = {norm(x) for x in marcas_cfg.get("multimarca_tipicas", [])}
    weights, avatares, eci = load_weights(), load_avatares(), load_eci_locations()
    brand_counts = count_stores_by_brand(rows, name_field="nombre")
    metrics = {"filtered": 0, "analyzed": 0, "stores": 0, "errors": 0}

    # Filtro de marcas excluidas (cadenas y competidores): no entran en el análisis.
    kept = []
    for row in rows:
        if is_excluded(row.get("nombre", ""), brands):
            metrics["filtered"] += 1
            LOGGER.info("[filtrado] %s", row.get("nombre", ""))
        else:
            kept.append(row)
    _emit_stats(metrics)

    # ── A. Webs ──
    hosts: Dict[str, str] = {}
    for row in kept:
        web = _norm_web(row.get("web", ""))
        if web and not is_social_url(web):
            hosts.setdefault(_host(web), web)
    web_cache = JsonCache(_CACHE_DIR / "webs.json", enabled=not args.refresh)
    webs = await _fase_webs(hosts, web_cache)

    # ── B. Reglas + Instagram para negocios sin web ──
    ig_creds = _dataforseo_creds()
    max_usd = args.dataforseo_max_usd if args.dataforseo_max_usd is not None else default_max_usd()
    if ig_budget is None and ig_creds:
        ig_budget = DataForSeoBudget(max_usd)
    ig_cache = SearchCache()
    llm_cache = JsonCache(_CACHE_DIR / "llm.json", enabled=not args.refresh)

    items = []
    for i, row in enumerate(kept):
        nombre = row.get("nombre", "")
        municipio = (row.get("municipio_origen") or "").strip()
        web = _norm_web(row.get("web", ""))
        host = _host(web) if web and not is_social_url(web) else ""
        entry = webs.get(host) if host else None
        sig = _signals_from(entry)
        es_cliente = norm(nombre).replace(" ", "") in clientes or any(c and c in norm(nombre).replace(" ", "") for c in clientes)
        clas = (Clasificacion("cliente_velfix", 1.0, ["está en config/clientes_velfix.json"])
                if es_cliente else classify(sig, nombre, marcas_cfg,
                                            n_tiendas_dataset=num_tiendas_for(nombre, brand_counts, municipio)))
        item = {"i": i, "row": row, "nombre": nombre, "municipio": municipio, "web": web,
                "host": host, "entry": entry, "sig": sig, "clas": clas,
                "instagram": (entry or {}).get("instagram", ""), "ig_fuente": "web" if (entry or {}).get("instagram") else "-",
                "snippets": []}
        item["sin_web"] = (not web or is_social_url(web)) and not es_cliente
        items.append(item)

    # Sin web propia (vacía o solo un enlace a Instagram/Facebook): fragmentos de Instagram.
    # 5 búsquedas a la vez (de una en una eran ~11 s cada una). ponytail: el límite de gasto
    # se comprueba antes de cada búsqueda, así que puede pasarse como mucho en 4 búsquedas (~$0,01).
    sin_web = [it for it in items if it["sin_web"]]
    if sin_web:
        LOGGER.info("Instagram: %d negocios sin web", len(sin_web))
        sem = asyncio.Semaphore(5)
        hechos = 0

        async def buscar(it: dict) -> None:
            nonlocal hechos
            async with sem:
                url, fuente, snippets = await resolve_instagram_with_snippets(
                    it["nombre"], it["municipio"], ig_creds, ig_budget, ig_cache)
            it.update(instagram=url, ig_fuente=fuente, snippets=snippets)
            hechos += 1
            if hechos % 50 == 0 or hechos == len(sin_web):
                LOGGER.info("Instagram: %d/%d", hechos, len(sin_web))

        await asyncio.gather(*(buscar(it) for it in sin_web))

    # ── C. LLM para dudosos ──
    casos, by_id = [], {}
    for it in items:
        c = it["clas"]
        if c.tipo_negocio == "cliente_velfix" or c.confianza >= UMBRAL_LLM:
            continue
        if it["sig"] is not None and it["sig"].ok:
            texto, key = it["sig"].texto, f"web:{it['host']}"
        elif it["snippets"]:
            texto = f"Instagram: {it['instagram'] or 'no localizado'}\nResultados de Google sobre su Instagram:\n" + "\n".join(it["snippets"])
            key = f"nw:{norm(it['nombre'])}|{norm(it['municipio'])}"
        else:
            continue  # sin datos: queda en revisión
        it["llm_key"] = key
        hit = llm_cache.get(key)
        if hit:
            it["llm"] = Clasificacion(**hit)
        elif key not in by_id:
            by_id[key] = str(len(by_id))
            casos.append({"id": by_id[key], "nombre": it["nombre"], "municipio": it["municipio"],
                          "categoria": it["row"].get("categoria", ""), "web": it["web"], "texto": texto})
    if casos and not args.no_llm:
        LOGGER.info("LLM: %d negocios dudosos a Claude Haiku en %d lotes", len(casos), -(-len(casos) // llm.BATCH))
        res = await llm.classify_with_llm(casos)
        if not res:
            LOGGER.warning("LLM sin respuestas: ¿sesión de 'claude' caducada? Ejecuta 'claude auth login'. Los dudosos quedan en revisión.")
        id_to_key = {v: k for k, v in by_id.items()}
        for cid, cl in res.items():
            if cid in id_to_key:
                llm_cache.put(id_to_key[cid], cl.to_dict())
        llm_cache.save()
    for it in items:
        if "llm_key" in it and "llm" not in it:
            hit = llm_cache.get(it["llm_key"]) if not args.no_llm else None
            if hit:
                it["llm"] = Clasificacion(**hit)
        if it.get("llm"):
            it["clas"] = llm.merge(it["clas"], it["llm"])

    # ── D. Puerta + scoring ──
    out = []
    for it in items:
        row, sig, entry, c = it["row"], it["sig"], it["entry"] or {}, it["clas"]
        nombre, municipio = it["nombre"], it["municipio"]
        es_tienda, tecnologia = entry.get("es_tienda", "-"), entry.get("tecnologia", "-")
        if es_tienda == "Sí":
            metrics["stores"] += 1
        if it["web"] and it["host"] and not (sig and sig.ok):
            metrics["errors"] += 1

        marcas = [m for m in (sig.marcas if sig else []) if norm(m) not in norm(nombre)]
        n_tip = sum(1 for m in marcas if norm(m) in tipicas)
        n_tiendas_web = sig.num_tiendas_web if sig else None
        evid_txt = "; ".join(c.evidencias)
        base = {k: v for k, v in row.items()}
        fila = {
            **base,
            "es_tienda": es_tienda, "tecnologia": tecnologia,
            "tipo_negocio": c.tipo_negocio, "confianza": round(c.confianza, 2), "evidencias": evid_txt,
            "num_marcas": len(marcas) if sig else "", "marcas_detectadas": ", ".join(marcas[:40]),
            "num_tiendas_web": n_tiendas_web if n_tiendas_web is not None else "",
            "provincias_tiendas": ", ".join(PROVINCIAS.get(p, p) for p in (sig.provincias if sig else [])),
            "sector_real": sig.sector_real if sig and sig.ok else "",
            "subsector": sig.subsector if sig and sig.ok else "",
            "publico": ", ".join(sig.publico) if sig and sig.ok else "",
        }
        etiqueta = _TIPO_LABEL.get(c.tipo_negocio, c.tipo_negocio)
        principal = c.evidencias[0] if c.evidencias else ""

        if c.tipo_negocio != "multimarca":
            revision = c.tipo_negocio == "dudoso"
            fila.update(motivo_descarte="" if revision else c.tipo_negocio,
                        revisar_manual="Sí" if revision else "",
                        prioridad="revision" if revision else "descartado", puntuacion="",
                        justificacion=f"{etiqueta} — {principal}")
        else:
            lat, lon = None, None
            if (row.get("lat") or "").strip() and (row.get("lon") or "").strip():
                try:
                    lat, lon = float(row["lat"]), float(row["lon"])
                except ValueError:
                    lat, lon = None, None
            if lat is None:
                lat, lon = coords_from_maps_url(row.get("maps_url", ""))
            n_tiendas = n_tiendas_web if n_tiendas_web else num_tiendas_for(nombre, brand_counts, municipio)
            if es_tienda == "Sí":
                madurez = "ecommerce_funcional"
            elif it["web"] and is_social_url(it["web"]):
                madurez = "solo_redes_sociales"
            elif it["web"]:
                madurez = "solo_redes_sociales" if sig and sig.ok else "web_no_accesible"
            else:
                madurez = "sin_presencia"
            ctx = {
                "lat": lat, "lon": lon,
                "poblacion": (get_poblacion_municipio(municipio) if municipio else None) or 0,
                "num_tiendas": n_tiendas, "madurez": madurez,
                "tecnologia": tecnologia if tecnologia not in ("", "-") else None,
                "num_marcas": len(marcas) if marcas else None, "n_tipicas": n_tip,
            }
            score = compute_score(ctx, eci_locations=eci, avatares=avatares, weights=weights)
            fila.update(motivo_descarte="", revisar_manual="",
                        prioridad=score["prioridad"], puntuacion=score["puntuacion_total"],
                        justificacion=f"{etiqueta} — {principal} | {score['justificacion']}")
        fila.update(
            email=entry.get("email") or fictitious_email(nombre),
            instagram=it["instagram"], instagram_fuente=it["ig_fuente"],
        )
        out.append(fila)
        metrics["analyzed"] += 1
        LOGGER.info("[analizado] %s | %s (%.2f, %s) → %s %s", nombre, c.tipo_negocio, c.confianza, c.fuente,
                    fila["prioridad"], fila["puntuacion"])
    _emit_stats(metrics)

    tipos: Dict[str, int] = {}
    prios: Dict[str, int] = {}
    for f in out:
        tipos[f["tipo_negocio"]] = tipos.get(f["tipo_negocio"], 0) + 1
        prios[str(f["prioridad"])] = prios.get(str(f["prioridad"]), 0) + 1
    LOGGER.info("ICP tipos: %s", json.dumps(tipos, ensure_ascii=False))
    LOGGER.info("ICP prioridades: %s", json.dumps(prios, ensure_ascii=False))
    if ig_budget is not None:
        LOGGER.info("DataForSEO: gastado $%.3f de $%.2f", ig_budget.spent, ig_budget.max_usd)
        if ig_budget.pending:
            LOGGER.warning("IGBUDGET spent=%.2f cap=%.2f pending=%d est=%.2f",
                           ig_budget.spent, ig_budget.max_usd, ig_budget.pending, ig_budget.estimated_extra_usd)
    return out


def write_xlsx(path: str, rows: List[dict], analysis_rows: List[dict]) -> None:
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Datos originales"
    if rows:
        headers = list(rows[0].keys())
        ws1.append(headers)
        for row in rows:
            ws1.append([row.get(h, "") for h in headers])
    ws2 = wb.create_sheet("Análisis")
    if analysis_rows:
        headers = [h for h in analysis_rows[0].keys() if h not in _ANALYSIS_DROP]
        ws2.append(headers)
        for row in analysis_rows:
            ws2.append([row.get(h, "") for h in headers])
    wb.save(path)


async def _run(args: argparse.Namespace) -> None:
    start = time.perf_counter()
    rows = _dedup_rows(_read_csv(args.csv_path))
    LOGGER.info("Registros en CSV: %d", len(rows))
    analysis_rows = await analyze(rows, args)
    xlsx = args.xlsx_path or str(Path(args.csv_path).with_suffix(".xlsx"))
    write_xlsx(xlsx, rows, analysis_rows)
    LOGGER.info("XLSX guardado: %s", xlsx)
    LOGGER.info("── Resumen análisis ── total=%d analizados=%d elapsed_s=%.1f",
                len(rows), len(analysis_rows), time.perf_counter() - start)


def _emit_stats(metrics: dict) -> None:
    LOGGER.info(
        "ASTATS filtered=%d analyzed=%d stores=%d errors=%d",
        metrics["filtered"], metrics["analyzed"], metrics["stores"], metrics["errors"],
    )


def main() -> None:
    setup_logging()
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
