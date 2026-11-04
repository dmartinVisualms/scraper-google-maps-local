"""Detección de perfil de Instagram del negocio.

Flujo:
1. Primero intenta extraer un handle del HTML ya descargado para fingerprint
   (regex sobre `instagram.com/<handle>`, sin dependencias nuevas).
2. Si no aparece ninguno (o no hay HTML), consulta DataForSEO SERP API con
   `site:instagram.com "nombre" "municipio"` para localizarlo.

Credenciales en `.env`:
    DATAFORSEO_LOGIN=email@tucuenta.com
    DATAFORSEO_PASSWORD=api_password
    DATAFORSEO_MODE=live   # 'live' ($2/1k, ~2s) o 'task' ($0.60/1k, ~10s) — no implementado todavía

Sin credenciales el fallback se salta silenciosamente (solo extracción de HTML).

ponytail: endpoint Live. Task queue ($0.60) añade polling y batch; a volúmenes
realistas (cientos de lookups por análisis) la diferencia es céntimos.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from typing import Optional

LOGGER = logging.getLogger(__name__)

# `instagram.com/<handle>` — handles IG: 1-30 chars alfanuméricos, punto, guión bajo.
_IG_URL_RE = re.compile(
    r"(?:https?:)?//(?:www\.)?instagram\.com/([A-Za-z0-9_.]{1,30})",
    re.IGNORECASE,
)

# Paths que parecen handles pero son rutas reservadas de IG.
_IG_RESERVED = frozenset({
    "p", "reel", "reels", "tv", "stories", "explore", "accounts",
    "direct", "about", "developer", "web", "_u", "legal", "sitemap",
    "press", "api", "download", "mobile", "challenge", "emails",
    "session", "ajax", "oauth", "privacy", "terms", "help",
})

_MAX_DATAFORSEO_PER_RUN = int(os.environ.get("DATAFORSEO_MAX_PER_RUN", "500"))


def _is_valid_handle(handle: str) -> bool:
    h = handle.strip("/").lower()
    if not h or h in _IG_RESERVED:
        return False
    # Rechazar basura obvia (handles reales son alfanum + . + _)
    return all(c.isalnum() or c in "._" for c in h)


def extract_instagram_from_html(html: Optional[str]) -> Optional[str]:
    """Busca el primer handle de IG válido en el HTML. Devuelve URL completa o None."""
    if not html:
        return None
    for m in _IG_URL_RE.finditer(html):
        handle = m.group(1).strip("/")
        if _is_valid_handle(handle):
            return f"https://instagram.com/{handle}"
    return None


# ── DataForSEO fallback ─────────────────────────────────────────────────────

_DATAFORSEO_URL = (
    "https://api.dataforseo.com/v3/serp/google/organic/live/regular"
)


def _dataforseo_creds() -> Optional[tuple]:
    """Devuelve (login, password) desde env/.env, o None si no hay credenciales."""
    # Delega en el loader existente para evitar duplicar parser de .env
    from src.engine_api.secrets import _read_env_file

    login = os.environ.get("DATAFORSEO_LOGIN", "").strip() or _read_env_file("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD", "").strip() or _read_env_file("DATAFORSEO_PASSWORD")
    if login and password:
        return (login, password)
    return None


class DataForSeoBudget:
    """Contador compartido que corta el fallback si se sobrepasa el cap del run."""

    def __init__(self, cap: int = _MAX_DATAFORSEO_PER_RUN) -> None:
        self.cap = max(0, cap)
        self.used = 0
        self._warned = False

    def try_charge(self) -> bool:
        if self.cap <= 0:
            return False
        if self.used >= self.cap:
            if not self._warned:
                LOGGER.warning(
                    "DataForSEO: cap alcanzado (%d queries) — saltando fallbacks restantes. "
                    "Ajusta DATAFORSEO_MAX_PER_RUN si quieres más.",
                    self.cap,
                )
                self._warned = True
            return False
        self.used += 1
        return True


async def search_instagram_via_dataforseo(
    nombre: str,
    municipio: str,
    creds: tuple,
    timeout_s: int = 30,
) -> Optional[str]:
    """Busca el perfil de IG con una query SERP. Devuelve URL o None."""
    import aiohttp

    nombre = (nombre or "").strip()
    if not nombre:
        return None

    parts = [f'"{nombre}"']
    municipio = (municipio or "").strip()
    if municipio:
        parts.append(f'"{municipio}"')
    query = f'site:instagram.com {" ".join(parts)}'

    login, password = creds
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    headers = {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }
    payload = [{
        "keyword": query[:500],
        "location_code": 2724,   # Spain
        "language_code": "es",
        "depth": 10,
    }]

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_DATAFORSEO_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    LOGGER.warning("DataForSEO HTTP %d para '%s'", resp.status, query)
                    return None
                data = await resp.json()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("DataForSEO error para '%s': %s", query, exc)
        return None

    tasks = data.get("tasks") or []
    if not tasks:
        return None
    result = tasks[0].get("result") or []
    if not result:
        return None
    items = result[0].get("items") or []
    for item in items:
        if item.get("type") != "organic":
            continue
        url = item.get("url") or ""
        m = _IG_URL_RE.search(url)
        if m and _is_valid_handle(m.group(1)):
            return f"https://instagram.com/{m.group(1).strip('/')}"
    return None


async def resolve_instagram(
    html: Optional[str],
    nombre: str,
    municipio: str,
    creds: Optional[tuple],
    budget: Optional[DataForSeoBudget],
) -> tuple:
    """Devuelve (instagram_url, fuente) donde fuente ∈ {'web', 'dataforseo', '-'}."""
    from_html = extract_instagram_from_html(html)
    if from_html:
        return (from_html, "web")
    if not creds or budget is None or not budget.try_charge():
        return ("", "-")
    found = await search_instagram_via_dataforseo(nombre, municipio, creds)
    if found:
        return (found, "dataforseo")
    return ("", "-")


# ── Self-check ──────────────────────────────────────────────────────────────

if __name__ == "__main__":  # ponytail: assert-based self-check
    # Extracción desde HTML
    assert extract_instagram_from_html(None) is None
    assert extract_instagram_from_html("") is None
    assert extract_instagram_from_html(
        '<a href="https://instagram.com/midwi.es">IG</a>'
    ) == "https://instagram.com/midwi.es"
    assert extract_instagram_from_html(
        '<a href="//www.instagram.com/foo_bar/">IG</a>'
    ) == "https://instagram.com/foo_bar"
    # Rechaza paths reservados
    assert extract_instagram_from_html(
        '<a href="https://instagram.com/p/abc123/">post</a>'
    ) is None
    assert extract_instagram_from_html(
        '<a href="https://instagram.com/reel/xyz">reel</a>'
    ) is None
    # Primer handle válido gana aunque haya ruido antes
    assert extract_instagram_from_html(
        '<a href="/explore">x</a><a href="https://instagram.com/acmecorp">IG</a>'
    ) == "https://instagram.com/acmecorp"
    # Rechaza handle vacío
    assert extract_instagram_from_html(
        '<a href="https://instagram.com/">IG</a>'
    ) is None
    # Budget
    b = DataForSeoBudget(cap=2)
    assert b.try_charge() and b.try_charge()
    assert not b.try_charge()
    b0 = DataForSeoBudget(cap=0)
    assert not b0.try_charge()
    print("instagram self-check OK")
