"""Paso con LLM para los negocios dudosos: Claude Haiku 5.5 vía `claude -p`.

Usa el login del plan Team de Claude Code (sin API key). Se lanza desde una
carpeta vacía para no cargar el CLAUDE.md ni los hooks del proyecto, y sin
ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY en el entorno para que use la suscripción.
Se envían varios negocios por llamada para gastar menos límite de uso.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Dict, List

from src.analyzer.icp.rules import TIPOS, Clasificacion

LOGGER = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"
# Haiku pone 0,7 a conclusiones con indicios débiles (p. ej. O'ruan → marca_propia por
# el nombre de la web); solo se acepta su decisión con confianza alta.
UMBRAL_ACEPTAR_LLM = 0.8
BATCH = 10
_CWD = Path(__file__).resolve().parents[3] / "cache" / "icp" / "llm_cwd"

SCHEMA = {
    "type": "object",
    "properties": {
        "resultados": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tipo_negocio": {"type": "string", "enum": list(TIPOS)},
                    "confianza": {"type": "number"},
                    "evidencias": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "tipo_negocio", "confianza", "evidencias"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["resultados"],
    "additionalProperties": False,
}

INSTRUCCIONES = """Clasifica cada negocio según qué vende. Es para una campaña comercial dirigida a tiendas MULTIMARCA de moda.

Tipos:
- multimarca: tienda de moda (ropa, calzado, deporte, infantil) que vende varias marcas de terceros; puede tener además algo de marca propia.
- marca_propia: diseña o vende principalmente su propia marca de moda.
- marca_propia_franquicia: marca propia con red de franquicias o tiendas en muchas provincias.
- franquiciado_monomarca: tienda de una cadena ajena que vende una sola marca que no es la suya.
- no_moda: no vende moda (perfumería, cosmética, óptica, joyería, hogar…), aunque Google la catalogue como tienda de ropa.
- dudoso: los datos no bastan para decidir.

Usa solo los datos de cada negocio; no supongas nada que no aparezca. Mencionar o etiquetar varias marcas de terceros (por ejemplo @marca en Instagram) es indicio de multimarca; hablar solo de "nuestra colección" sin marcas ajenas apunta a marca propia. Si no hay pruebas claras, responde dudoso con confianza baja. La confianza va de 0 a 1. En evidencias pon de 1 a 3 frases cortas que citen el dato que decide.

Devuelve un resultado por cada id."""


def build_prompt(casos: List[dict]) -> str:
    bloques = []
    for c in casos:
        bloques.append(
            f'<negocio id="{c["id"]}">\n'
            f'Nombre: {c["nombre"]}\nMunicipio: {c.get("municipio", "")}\n'
            f'Categoría en Google Maps: {c.get("categoria", "")}\nWeb: {c.get("web") or "sin web"}\n'
            f'{c.get("texto", "").strip()}\n</negocio>'
        )
    return INSTRUCCIONES + "\n\n" + "\n\n".join(bloques)


def parse_response(stdout: str) -> Dict[str, Clasificacion]:
    """Convierte la salida JSON de `claude -p` en {id: Clasificacion}. {} si hay error."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return {}
    if data.get("is_error") or not data.get("structured_output"):
        LOGGER.warning("Paso LLM sin resultado: %s", str(data.get("result", ""))[:200])
        return {}
    out = {}
    for r in data["structured_output"].get("resultados", []):
        if r.get("tipo_negocio") in TIPOS:
            conf = max(0.0, min(1.0, float(r.get("confianza", 0))))
            out[str(r["id"])] = Clasificacion(r["tipo_negocio"], conf, list(r.get("evidencias", []))[:3], "llm")
    return out


async def _run_batch(casos: List[dict], timeout_s: int = 180) -> Dict[str, Clasificacion]:
    _CWD.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY")}
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", "--model", MODEL, "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(_CWD), env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(build_prompt(casos).encode()), timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        LOGGER.warning("Paso LLM: tiempo agotado en un lote de %d negocios", len(casos))
        return {}
    return parse_response(stdout.decode(errors="replace"))


async def classify_with_llm(casos: List[dict], concurrency: int = 3) -> Dict[str, Clasificacion]:
    """casos: [{id, nombre, municipio, categoria, web, texto}]. Devuelve {id: Clasificacion}."""
    lotes = [casos[i:i + BATCH] for i in range(0, len(casos), BATCH)]
    sem = asyncio.Semaphore(concurrency)

    async def run(lote):
        async with sem:
            return await _run_batch(lote)

    out: Dict[str, Clasificacion] = {}
    for res in await asyncio.gather(*(run(lote) for lote in lotes)):
        out.update(res)
    return out


def merge(reglas: Clasificacion, llm: "Clasificacion | None") -> Clasificacion:
    """El LLM solo sustituye a las reglas si decide algo distinto de dudoso con confianza suficiente."""
    if llm and llm.tipo_negocio != "dudoso" and llm.confianza >= UMBRAL_ACEPTAR_LLM:
        return Clasificacion(llm.tipo_negocio, llm.confianza, llm.evidencias + reglas.evidencias[:1], "llm")
    if llm:
        return Clasificacion("dudoso", min(reglas.confianza, llm.confianza),
                             reglas.evidencias + [f"LLM: {e}" for e in llm.evidencias[:1]], "reglas+llm")
    return reglas
