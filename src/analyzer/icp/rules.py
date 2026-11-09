"""Clasificación determinista del tipo de negocio a partir de las señales de su web.

Tipos: multimarca, marca_propia, marca_propia_franquicia, franquiciado_monomarca,
no_moda, dudoso. Los dudosos o con confianza < 0,7 pasan después por el LLM.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from src.analyzer.icp.signals import Signals

_CONFIG = Path(__file__).resolve().parents[3] / "config" / "icp_marcas.json"

TIPOS = ("multimarca", "marca_propia", "marca_propia_franquicia",
         "franquiciado_monomarca", "no_moda", "dudoso")
UMBRAL_LLM = 0.7


@dataclass
class Clasificacion:
    tipo_negocio: str
    confianza: float
    evidencias: List[str] = field(default_factory=list)
    fuente: str = "reglas"  # reglas | llm

    def to_dict(self) -> dict:
        return asdict(self)


def load_marcas_config(path: Optional[Path] = None) -> dict:
    return json.loads((path or _CONFIG).read_text(encoding="utf-8"))


def norm(s: str) -> str:
    from unidecode import unidecode
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", unidecode(s or "").lower())).strip()


def _is_own_name(brand: str, nombre: str) -> bool:
    b, n = norm(brand), norm(nombre)
    return bool(b) and (b == n or b in n or n in b)


def _in_list(brand: str, lista: set) -> bool:
    b = norm(brand)
    return b in lista or any(len(x) >= 4 and (x in b or b in x) for x in lista)


def _ev(texto: str, url: str = "") -> str:
    return f"{texto} ({url})" if url else texto


# Si el título de la web nombra uno de estos sectores, el negocio no es de moda
# aunque venda calzado o ropa (p. ej. una ortopedia con 57 marcas).
_TITULO_NO_MODA = ["ortopedia", "farmacia", "optica", "perfumeria", "joyeria", "relojeria",
                   "drogueria", "guarderia", "jugueteria", "puericultura"]
CADENA_MIN = 8  # locales a partir de los cuales es una cadena, fuera de esta campaña


def _cadena(n: int, fuente: str, url: str = "") -> Clasificacion:
    return Clasificacion("marca_propia", 0.75, [_ev(
        f"cadena: {n} tiendas con el mismo nombre {fuente} y ninguna marca de terceros en su web", url)])


def classify(sig: Optional[Signals], nombre: str, cfg: dict,
             excluded_brands: Optional[List[str]] = None, n_tiendas_dataset: int = 0) -> Clasificacion:
    """`n_tiendas_dataset`: locales con el mismo nombre en el CSV; se usa cuando la web no
    muestra marcas ni página de tiendas (cadenas como Pilar Prieto o Punt Roma)."""
    excluded = [b for b in (excluded_brands or []) if norm(b) and norm(b) in norm(nombre)]
    if excluded:
        return Clasificacion("franquiciado_monomarca", 0.9, [f"nombre coincide con la cadena «{excluded[0]}»"])

    if sig is None or not sig.ok:
        if n_tiendas_dataset >= CADENA_MIN:
            return _cadena(n_tiendas_dataset, "en el dataset (sin web)")
        return Clasificacion("dudoso", 0.3, ["sin web accesible"])

    url = sig.final_url
    titulo = norm(sig.title)
    sector_titulo = next((t for t in _TITULO_NO_MODA if norm(t) in titulo), None)
    if sector_titulo:
        return Clasificacion("no_moda", 0.85, [_ev(f"el título de la web es de {sector_titulo}: «{sig.title[:80]}»", url)])
    tipicas = {norm(x) for x in cfg.get("multimarca_tipicas", [])}
    no_moda_set = {norm(x) for x in cfg.get("no_moda", [])}

    marcas_no_moda = [m for m in sig.marcas if _in_list(m, no_moda_set)]
    if sig.sector_real != "moda" or (len(marcas_no_moda) >= 3 and len(marcas_no_moda) * 2 >= len(sig.marcas)):
        ev = []
        if sig.sector_real != "moda":
            ev.append(_ev(f"vocabulario dominante de {sig.sector_real} ({sig.sector_scores.get(sig.sector_real, 0)} menciones frente a {sig.sector_scores.get('moda', 0)} de moda)", url))
        if marcas_no_moda:
            ev.append(_ev(f"marcas de {len(marcas_no_moda)} firmas de perfumería/cosmética: {', '.join(marcas_no_moda[:6])}", sig.marcas_url))
        return Clasificacion("no_moda", 0.9 if len(ev) == 2 else 0.75, ev)

    n_tiendas = sig.num_tiendas_web or 0
    if sig.franquicia and (n_tiendas > 7 or len(sig.provincias) > 2):
        return Clasificacion("marca_propia_franquicia", 0.9, [
            _ev(f"textos de franquicia: {', '.join(sig.franquicia[:3])}", url),
            _ev(f"{n_tiendas} tiendas en {len(sig.provincias)} provincias", sig.tiendas_url),
        ] + ([_ev(f"canal profesional: {', '.join(sig.b2b[:2])}", url)] if sig.b2b else []))

    terceros = [m for m in sig.marcas if not _is_own_name(m, nombre)]
    tip = [m for m in terceros if _in_list(m, tipicas)]
    if len(terceros) >= 5 or len(tip) >= 3:
        conf = 0.9 if (len(terceros) >= 10 or len(tip) >= 5) else 0.8
        return Clasificacion("multimarca", conf, [
            _ev(f"{len(terceros)} marcas de terceros en el catálogo: {', '.join((tip or terceros)[:8])}", sig.marcas_url),
        ] + ([_ev(f"{n_tiendas} tiendas", sig.tiendas_url)] if n_tiendas else []))

    solo_propia = len(sig.marcas) == 1 and _is_own_name(sig.marcas[0], nombre)
    if solo_propia or (not terceros and (sig.propia_fuerte or (n_tiendas >= 5 and sig.propia_debil) or n_tiendas > 7)):
        ev = []
        if solo_propia:
            ev.append(_ev(f"la única marca del catálogo es la propia ({sig.marcas[0]})", sig.marcas_url))
        if sig.propia_fuerte or sig.propia_debil:
            ev.append(_ev(f"textos de colección propia: {', '.join((sig.propia_fuerte + sig.propia_debil)[:3])}", url))
        if n_tiendas:
            ev.append(_ev(f"{n_tiendas} tiendas propias en {len(sig.provincias)} provincias y ninguna marca de terceros", sig.tiendas_url))
        return Clasificacion("marca_propia", 0.85 if (solo_propia or sig.propia_fuerte) else 0.75, ev)

    if not terceros and n_tiendas_dataset >= CADENA_MIN:
        return _cadena(n_tiendas_dataset, "en el dataset", url)

    if terceros:
        return Clasificacion("dudoso", 0.5, [_ev(f"solo {len(terceros)} marcas de terceros: {', '.join(terceros)}", sig.marcas_url)])
    ev = ["la web no nombra ninguna marca"]
    if n_tiendas:
        ev.append(_ev(f"{n_tiendas} tiendas", sig.tiendas_url))
    return Clasificacion("dudoso", 0.4, ev)
