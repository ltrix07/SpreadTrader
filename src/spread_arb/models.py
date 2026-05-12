from __future__ import annotations

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
