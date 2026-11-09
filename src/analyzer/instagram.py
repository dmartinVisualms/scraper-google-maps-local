"""Detección de perfil de Instagram del negocio.

Flujo:
1. Extrae un handle del HTML ya descargado para fingerprint (gratis).
2. Si no aparece, busca en Google vía DataForSEO SERP API
   (`site:instagram.com "nombre" "municipio"`), con límite de gasto en USD
   por ejecución y caché en disco para no pagar dos veces la misma búsqueda.

Google ya no devuelve la URL real en los resultados (viene envuelta y cifrada
en `google.com/goto?url=...`), así que el handle se saca del título del
resultado de perfil: "Thebes Moda (@thebesmoda) - Instagram".

Credenciales en `.env`: DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD.
Límite: DATAFORSEO_MAX_USD_PER_RUN (default 1.0) o `--dataforseo-max-usd`.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

LOGGER = logging.getLogger(__name__)

_IG_URL_RE = re.compile(
    r"(?:https?:)?//(?:www\.)?instagram\.com/([A-Za-z0-9_.]{1,30})",
    re.IGNORECASE,
)
# Título de resultado de perfil: "Nombre (@handle)" con o sin " - Instagram" detrás.
_IG_TITLE_RE = re.compile(r"^(.*?)\s*\(@([A-Za-z0-9_.]{1,30})\)")
# Títulos "(@handle)" de otras redes que hay que descartar.
_OTHER_NETWORKS = ("tiktok", "threads", "mentions", "facebook", "twitter", "youtube", "pinterest", "linkedin")
# breadcrumb "https://www.instagram.com › handle"
_IG_BREADCRUMB_RE = re.compile(r"instagram\.com\s*›\s*([A-Za-z0-9_.]{1,30})\s*$", re.IGNORECASE)

_IG_RESERVED = frozenset({
    "p", "reel", "reels", "tv", "stories", "explore", "accounts",
    "direct", "about", "developer", "web", "_u", "legal", "sitemap",
    "press", "api", "download", "mobile", "challenge", "emails",
    "session", "ajax", "oauth", "privacy", "terms", "help", "popular",
})

# Palabras demasiado genéricas para confirmar que un perfil es del negocio.
_GENERIC_TOKENS = frozenset({
    "moda", "modas", "tienda", "tiendas", "calzado", "calzados", "zapateria",
    "zapaterias", "boutique", "ropa", "shop", "store", "sport", "sports",
    "deporte", "deportes", "kids", "ninos", "infantil", "outlet", "the",
    "los", "las", "del", "de", "y", "la", "el", "and", "mujer", "hombre",
})

_DATAFORSEO_URL = "https://api.dataforseo.com/v3/serp/google/organic/live/regular"
# Medido en nov-2026: $0.002 por consulta sin operadores; con site: o comillas, $0.01.
# Por eso la consulta va sin operadores. El coste real se lee de cada respuesta.
_COST_PER_QUERY_ESTIMATE = 0.002
_CACHE_PATH = Path(__file__).resolve().parents[2] / "cache" / "instagram_search.json"


def _is_valid_handle(handle: str) -> bool:
    h = handle.strip("/").lower()
    if not h or h in _IG_RESERVED or ".." in h or h.endswith("."):  # ".." = título cortado por Google
        return False
    return all(c.isalnum() or c in "._" for c in h)


# Palabras que pueden acompañar al nombre en un handle sin indicar otro negocio.
_HANDLE_FILLER = _GENERIC_TOKENS | {"spain", "es", "esp", "oficial", "official", "online", "by", "galicia", "asturias"}


def _norm(s: str) -> str:
    from unidecode import unidecode
    return re.sub(r"[^a-z0-9 ]+", " ", unidecode(s or "").lower())


def _significant_tokens(nombre: str) -> list:
    toks = [t for t in _norm(nombre).split() if len(t) >= 3 and t not in _GENERIC_TOKENS]
    return toks or [t for t in _norm(nombre).split() if len(t) >= 3]


def matches_business(nombre: str, handle: str, profile_name: str = "") -> bool:
    """True si algún token significativo del nombre aparece en el handle o en el
    nombre del perfil. Evita asignar el Instagram de otro negocio."""
    toks = _significant_tokens(nombre)
    if not toks:
        return False
    hay = _norm(handle).replace(" ", "") + " " + _norm(profile_name)
    return any(t in hay for t in toks)


def extract_instagram_from_html(html: Optional[str]) -> Optional[str]:
    """Primer handle de IG válido en el HTML del negocio, como URL completa."""
    if not html:
        return None
    for m in _IG_URL_RE.finditer(html):
        handle = m.group(1).strip("/")
        if _is_valid_handle(handle):
            return f"https://instagram.com/{handle}"
    return None


def _candidate_score(nombre: str, municipio: str, handle: str, profile_name: str) -> int:
    """Puntúa un perfil candidato: +2 por cada palabra del nombre que aparece en el
    handle o en el nombre del perfil (también con letras separadas, "D A N D A R A"),
    −1 por cada palabra sobrante del handle (otro municipio, un apellido...)."""
    toks = _significant_tokens(nombre)
    flat_profile = _norm(profile_name).replace(" ", "")
    flat_handle = _norm(handle).replace(" ", "")
    hits = sum(1 for t in toks if t in flat_handle or t in flat_profile)
    if not hits:
        return 0
    muni = _norm(municipio).split()
    allowed = set(toks) | set(muni) | _HANDLE_FILLER
    # Una palabra del handle no sobra si es de las permitidas o contiene una del
    # nombre o del municipio ("dandaraoficial", "thebes.aestrada"); solo palabras de ≥4 letras.
    extras = [w for w in re.split(r"[^a-z]+", _norm(handle)) if len(w) >= 3
              and w not in allowed and not any(len(t) >= 4 and t in w for t in toks + muni)]
    # Con palabras sobrantes (otro municipio, un apellido) se descarta: mejor sin
    # Instagram que con el de otro negocio.
    return 0 if extras else 2 * hits


def handle_from_serp_items(items: list, nombre: str, municipio: str = "") -> Optional[str]:
    """Entre los resultados de perfil, devuelve el handle que mejor encaja con el negocio."""
    best, best_score = None, 0
    for item in items:
        if item.get("type") != "organic":
            continue
        title = item.get("title") or ""
        cands = []
        m = _IG_TITLE_RE.search(title)
        if m and not any(n in title.lower() for n in _OTHER_NETWORKS):
            cands.append((m.group(2), m.group(1)))
        for text in (item.get("url") or "", item.get("breadcrumb") or ""):
            m = _IG_URL_RE.search(text) or _IG_BREADCRUMB_RE.search(text)
            if m:
                cands.append((m.group(1), title))
        for handle, profile_name in cands:
            if not _is_valid_handle(handle):
                continue
            score = _candidate_score(nombre, municipio, handle, profile_name)
            if score > best_score:
                best, best_score = handle, score
    return best


# ── Credenciales, presupuesto y caché ──────────────────────────────────────

def _dataforseo_creds() -> Optional[tuple]:
    from src.engine_api.secrets import _read_env_file

    login = os.environ.get("DATAFORSEO_LOGIN", "").strip() or _read_env_file("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD", "").strip() or _read_env_file("DATAFORSEO_PASSWORD")
    return (login, password) if login and password else None


def default_max_usd() -> float:
    from src.engine_api.secrets import _read_env_file

    raw = os.environ.get("DATAFORSEO_MAX_USD_PER_RUN", "").strip() or _read_env_file("DATAFORSEO_MAX_USD_PER_RUN")
    try:
        return float(raw) if raw else 1.0
    except ValueError:
        return 1.0


class DataForSeoBudget:
    """Límite de gasto en USD por ejecución. Cuenta además los negocios que se
    quedaron sin buscar al agotarse, para poder pedir ampliación."""

    def __init__(self, max_usd: float = 1.0) -> None:
        self.max_usd = max(0.0, max_usd)
        self.spent = 0.0
        self.pending = 0
        self.last_cost = _COST_PER_QUERY_ESTIMATE
        self._warned = False

    def can_spend(self) -> bool:
        if self.spent + self.last_cost <= self.max_usd + 1e-9:
            return True
        self.pending += 1
        if not self._warned:
            LOGGER.warning(
                "DataForSEO: límite de $%.2f alcanzado — el resto de búsquedas de Instagram se omiten.",
                self.max_usd,
            )
            self._warned = True
        return False

    def charge(self, cost: float) -> None:
        self.spent += cost
        if cost > 0:
            self.last_cost = cost

    @property
    def estimated_extra_usd(self) -> float:
        return round(self.pending * self.last_cost + 0.004, 2)


class SearchCache:
    """Caché en disco de búsquedas ya pagadas (incluye las sin resultado)."""

    def __init__(self, path: Path = _CACHE_PATH) -> None:
        self.path = path
        try:
            self._data: Dict[str, dict] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}

    @staticmethod
    def key(nombre: str, municipio: str) -> str:
        return f"{_norm(nombre).strip()}|{_norm(municipio).strip()}"

    def get(self, nombre: str, municipio: str) -> Optional[dict]:
        return self._data.get(self.key(nombre, municipio))

    def put(self, nombre: str, municipio: str, url: str, snippets: Optional[list] = None) -> None:
        entry = {"url": url, "ts": int(time.time())}
        if snippets is not None:
            entry["snippets"] = snippets
        self._data[self.key(nombre, municipio)] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")


# ── Búsqueda ───────────────────────────────────────────────────────────────

async def search_instagram_via_dataforseo(
    nombre: str, municipio: str, creds: tuple, timeout_s: int = 30,
) -> Tuple[Optional[str], float]:
    """Busca el perfil de IG. Devuelve (url_o_None, coste_usd). Coste 0 si falló la llamada."""
    url, cost, _ = await search_instagram_serp(nombre, municipio, creds, timeout_s)
    return url, cost


async def search_instagram_serp(
    nombre: str, municipio: str, creds: tuple, timeout_s: int = 30,
) -> Tuple[Optional[str], float, list]:
    """Como search_instagram_via_dataforseo, pero devuelve además los fragmentos de texto
    de los resultados (bio y posts), que el analizador ICP usa con los negocios sin web."""
    import aiohttp

    nombre = (nombre or "").strip()
    if not nombre:
        return None, 0.0
    query = f"{nombre} {(municipio or '').strip()} instagram".replace("  ", " ")

    token = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
    payload = [{"keyword": query[:500], "location_code": 2724, "language_code": "es", "depth": 10}]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as session:
            async with session.post(_DATAFORSEO_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    LOGGER.warning("DataForSEO HTTP %d para '%s'", resp.status, query)
                    return None, 0.0, []
                data = await resp.json()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("DataForSEO error para '%s': %s", query, exc)
        return None, 0.0, []

    tasks = data.get("tasks") or [{}]
    cost = float(tasks[0].get("cost") or data.get("cost") or 0.0)
    result = tasks[0].get("result") or [{}]
    items = (result[0] or {}).get("items") or []
    handle = handle_from_serp_items(items, nombre, municipio)
    snippets = [
        f"{(i.get('title') or '').strip()} — {(i.get('description') or '').strip()}"
        for i in items if i.get("type") == "organic"
    ][:10]
    return (f"https://instagram.com/{handle}" if handle else None), cost, snippets


async def resolve_instagram_with_snippets(
    nombre: str, municipio: str, creds: Optional[tuple],
    budget: Optional[DataForSeoBudget], cache: Optional[SearchCache] = None,
) -> Tuple[str, str, list]:
    """Para negocios sin web: (instagram_url, fuente, fragmentos). Reutiliza la caché
    si ya tiene fragmentos; si solo tiene la URL (búsqueda de la versión normal), repite."""
    if cache is not None:
        hit = cache.get(nombre, municipio)
        if hit is not None and "snippets" in hit:
            return hit["url"], ("dataforseo" if hit["url"] else "-"), hit["snippets"]
    if not creds or budget is None or not budget.can_spend():
        return "", "-", []
    found, cost, snippets = await search_instagram_serp(nombre, municipio, creds)
    budget.charge(cost)
    if cost > 0 and cache is not None:
        cache.put(nombre, municipio, found or "", snippets)
    return (found or ""), ("dataforseo" if found else "-"), snippets


async def resolve_instagram(
    html: Optional[str],
    nombre: str,
    municipio: str,
    creds: Optional[tuple],
    budget: Optional[DataForSeoBudget],
    cache: Optional[SearchCache] = None,
) -> tuple:
    """Devuelve (instagram_url, fuente) con fuente ∈ {'web', 'dataforseo', '-'}."""
    from_html = extract_instagram_from_html(html)
    if from_html:
        return (from_html, "web")
    if cache is not None:
        hit = cache.get(nombre, municipio)
        if hit is not None:
            return (hit["url"], "dataforseo" if hit["url"] else "-")
    if not creds or budget is None or not budget.can_spend():
        return ("", "-")
    found, cost = await search_instagram_via_dataforseo(nombre, municipio, creds)
    budget.charge(cost)
    if cost > 0 and cache is not None:
        cache.put(nombre, municipio, found or "")
    return (found, "dataforseo") if found else ("", "-")


if __name__ == "__main__":  # ponytail: assert-based self-check
    assert extract_instagram_from_html('<a href="https://instagram.com/midwi.es">') == "https://instagram.com/midwi.es"
    assert extract_instagram_from_html('<a href="https://instagram.com/p/abc/">') is None
    items = [
        {"type": "organic", "title": "Vestido de @uniqueunlimited_spain ◽Jueves", "url": "https://google.com/goto?url=x", "breadcrumb": "Más de 50 Me gusta"},
        {"type": "organic", "title": "Thebes Moda (@thebesmoda) - Instagram", "url": "https://google.com/goto?url=y", "breadcrumb": "Más de 3,4 mil seguidores"},
    ]
    assert handle_from_serp_items(items, "Thebes") == "thebesmoda"
    assert handle_from_serp_items(items, "Calzados Raquel") is None   # perfil de otro negocio
    assert handle_from_serp_items([{"type": "organic", "title": "x", "breadcrumb": "https://www.instagram.com › popular › thebes"}], "Thebes") is None
    assert matches_business("Dándara", "dandaraoficial")
    assert not matches_business("Moda Tienda", "zara")
    assert handle_from_serp_items([{"type": "organic", "title": "Thebes Moda (@thebesmoda)"}], "Thebes") == "thebesmoda"
    assert handle_from_serp_items([{"type": "organic", "title": "Thebes Moda (@thebes.aestrada) - Mentions"}], "Thebes") is None
    b = DataForSeoBudget(max_usd=0.02)
    assert b.can_spend(); b.charge(0.01); assert b.can_spend(); b.charge(0.01)
    assert not b.can_spend() and b.pending == 1
    print("instagram self-check OK")
