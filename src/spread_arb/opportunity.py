from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import ExchangeName


@dataclass(frozen=True, slots=True)
class OpportunityDecision:
    status: str
    reason: str | None


@dataclass(slots=True)
class SpreadOpportunity:
    symbol: str
    long_exchange: ExchangeName
    short_exchange: ExchangeName
    ask_long: float
    bid_short: float
    raw_spread_pct: float
    estimated_net_spread_pct: float
    estimated_roundtrip_cost_pct: float
    available_long_size: float
    available_short_size: float
    quote_age_ms_long: float
    quote_age_ms_short: float
    notional_usdt: float
    status: str
    reason: str | None
    timestamp: datetime
    opportunity_id: int | None = None


def classify_opportunity(
    *,
    estimated_net_spread_pct: float,
    entry_net_spread_pct: float,
    is_fresh: bool,
    is_liquid: bool,
) -> OpportunityDecision:
    if estimated_net_spread_pct < entry_net_spread_pct:
        return OpportunityDecision(status="rejected", reason="net_spread_too_low")
    if not is_fresh:
        return OpportunityDecision(status="rejected", reason="stale_quote")
    if not is_liquid:
        return OpportunityDecision(status="rejected", reason="insufficient_liquidity")
    return OpportunityDecision(status="observed", reason=None)
