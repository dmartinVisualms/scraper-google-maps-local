from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_PRICING_PATH = Path(__file__).resolve().parent / "pricing.json"


@dataclass
class CostEstimate:
    tier: str
    requests: int
    billable_requests: int
    est_cost_usd: float
    rate_per_1000: float


def _load_pricing(pricing: Optional[dict] = None) -> dict:
    if pricing is not None:
        return pricing
    return json.loads(_PRICING_PATH.read_text(encoding="utf-8"))


def estimate_cost(
    requests: int,
    *,
    tier: str = "enterprise",
    free_remaining: Optional[int] = None,
    pricing: Optional[dict] = None,
) -> CostEstimate:
    data = _load_pricing(pricing)
    tier_cfg = data["text_search"][tier]
    free = tier_cfg["free_per_month"] if free_remaining is None else free_remaining
    band = tier_cfg["first_paid_band"]
    rate = band["rate_per_1000"] if band else 0.0
    billable = max(0, requests - free)
    cost = (billable / 1000.0) * rate
    return CostEstimate(
        tier=tier,
        requests=requests,
        billable_requests=billable,
        est_cost_usd=round(cost, 4),
        rate_per_1000=rate,
    )


def free_cap(tier: str = "enterprise", *, pricing: Optional[dict] = None) -> int:
    """Cuota gratis mensual del SKU (Enterprise=1000 con los campos que pedimos)."""
    return int(_load_pricing(pricing)["text_search"][tier]["free_per_month"])
