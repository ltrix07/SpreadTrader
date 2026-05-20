from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Symbol = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=3, max_length=32, pattern=r"^[A-Z0-9]+$"),
]


class ExchangeName(StrEnum):
    MEXC = "mexc"
    BYBIT = "bybit"
    OKX = "okx"
    BINANCE = "binance"
    GATE = "gate"
    BITGET = "bitget"
    HTX = "htx"


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    exchange: ExchangeName
    symbol: Symbol

    best_bid_price: Decimal = Field(gt=Decimal("0"))
    best_bid_size: Decimal = Field(ge=Decimal("0"))
    best_ask_price: Decimal = Field(gt=Decimal("0"))
    best_ask_size: Decimal = Field(ge=Decimal("0"))

    receive_latency_ms: float = Field(ge=0)
    source_latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Result of a single exchange order execution."""

    exchange: ExchangeName
    symbol: str
    side: str
    filled_qty: Decimal
    avg_price: Decimal
    fee: Decimal
    fee_currency: str
    order_id: str
    timestamp: datetime
    is_partial: bool
    raw_response: dict


@dataclass(frozen=True, slots=True)
class SpreadOrderResult:
    """Result of a spread entry or exit (two legs)."""

    long_order: OrderResult
    short_order: OrderResult


@dataclass(frozen=True, slots=True)
class PositionInfo:
    """Current position on an exchange."""

    exchange: ExchangeName
    symbol: str
    size: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    leverage: int


@dataclass(frozen=True, slots=True)
class BalanceInfo:
    """USDT balance on an exchange."""

    exchange: ExchangeName
    total_usdt: Decimal
    available_usdt: Decimal
