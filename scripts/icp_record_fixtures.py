"""Graba las webs de los casos de prueba ICP para que los tests funcionen sin red.

Uso: python -m scripts.icp_record_fixtures
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp

from src.analyzer.icp.fetch import USER_AGENT, Recorder, make_live_fetcher
from src.analyzer.icp.signals import extract_signals

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "icp"
CASES = {
    "dandara": "http://www.dandara.es/",
    "marta_barcala": "https://www.martabarcala.com/",
    "muchas": "http://bit.ly/3olfSTb",
    "oruan": "http://www.oruan.es/",
    "thebes": "http://www.thebesmoda.com/",
}


async def main() -> None:
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        for slug, web in CASES.items():
            rec = Recorder(make_live_fetcher(session))
            sig, _ = await extract_signals(web, rec)
            rec.save(FIXTURES / f"{slug}.json.gz")
            print(f"{slug}: {len(rec.pages)} páginas | marcas={len(sig.marcas)} tiendas={sig.num_tiendas_web} sector={sig.sector_real}")


if __name__ == "__main__":
    asyncio.run(main())
