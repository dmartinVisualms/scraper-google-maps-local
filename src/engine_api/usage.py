"""Contador mensual local de peticiones a la Places API.

ponytail: estimación local en out/api_usage_YYYY-MM.json para el indicador de
cuota en la UI. La fuente de verdad real es GCP (billing + budget alert).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from src.engine_api.cost import estimate_cost, free_cap

_USAGE_DIR = Path("out")


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _path(month: Optional[str] = None) -> Path:
    return _USAGE_DIR / f"api_usage_{month or _month_key()}.json"


def record_and_report(run_requests: int, *, tier: str = "enterprise") -> Dict:
    """Suma run_requests al contador del mes en curso y devuelve el estado de cuota."""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    prev = json.loads(p.read_text(encoding="utf-8")).get("requests", 0) if p.exists() else 0
    month_requests = prev + run_requests
    p.write_text(json.dumps({"month": _month_key(), "requests": month_requests}), encoding="utf-8")

    cap = free_cap(tier)
    cost = estimate_cost(month_requests, tier=tier)
    return {
        "run_requests": run_requests,
        "month_requests": month_requests,
        "free_cap": cap,
        "remaining": max(0, cap - month_requests),
        "within_free": month_requests <= cap,
        "est_cost_usd": cost.est_cost_usd,
        "month": _month_key(),
    }


if __name__ == "__main__":  # ponytail: self-check, no framework
    import tempfile
    _USAGE_DIR = Path(tempfile.mkdtemp())
    a = record_and_report(600)
    b = record_and_report(600)  # acumula -> 1200, supera el cap de 1000
    assert a["month_requests"] == 600 and a["within_free"] is True, a
    assert b["month_requests"] == 1200 and b["within_free"] is False, b
    assert b["est_cost_usd"] == round((200 / 1000) * 35.0, 4), b  # 200 facturables * $35/1000
    print("usage self-check OK", b)
