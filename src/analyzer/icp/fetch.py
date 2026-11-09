"""Descarga de páginas para la clasificación ICP, con grabación/reproducción para tests.

Un *fetcher* es una función async `fetch(url) -> (url_final, html | None)`.
- `make_live_fetcher(session)`: descarga real con aiohttp.
- `Recorder(fetcher)`: envuelve otro fetcher y guarda todo lo descargado.
- `make_replay_fetcher(path)`: reproduce lo grabado, sin red (tests).
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, Tuple

FetchResult = Tuple[str, Optional[str]]
Fetcher = Callable[[str], Awaitable[FetchResult]]

# UA de navegador: varias webs de tiendas devuelven 403 a UAs de bot.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)


def make_live_fetcher(session, timeout_s: int = 15) -> Fetcher:
    import aiohttp

    async def fetch(url: str) -> FetchResult:
        try:
            async with session.get(
                url, ssl=False, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                final = str(resp.url)
                ctype = resp.headers.get("content-type", "")
                if resp.status >= 400 or not ("html" in ctype or "json" in ctype):
                    return final, None
                return final, await resp.text(errors="replace")
        except Exception:  # noqa: BLE001 — una web caída no debe parar el análisis
            return url, None

    return fetch


class Recorder:
    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher
        self.pages: Dict[str, list] = {}

    async def __call__(self, url: str) -> FetchResult:
        final, html = await self._fetcher(url)
        self.pages[url] = [final, html]
        return final, html

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(self.pages, fh, ensure_ascii=False)


def make_replay_fetcher(path: Path) -> Fetcher:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        pages: Dict[str, list] = json.load(fh)

    async def fetch(url: str) -> FetchResult:
        final, html = pages.get(url, [url, None])
        return final, html

    return fetch
