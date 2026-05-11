from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import ExchangeClient


class BybitExchange(ExchangeClient):
    base_url = "https://api.bybit.com"

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.BYBIT

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        endpoint = f"{self.base_url}/v5/market/tickers"
        params = {"category": "linear", "symbol": symbol}

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit API error: {payload}")

        tickers = payload.get("result", {}).get("list", [])
        if not tickers:
            raise RuntimeError(f"Bybit returned no ticker for symbol={symbol}")

        ticker = tickers[0]
        server_ms = payload.get("time")
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = float(received_at_ms - server_ms) if isinstance(server_ms, int) else None

        return Quote(
            received_at=received_at,
            exchange=self.name,
            symbol=symbol,
            best_bid_price=Decimal(ticker["bid1Price"]),
            best_bid_size=Decimal(ticker["bid1Size"]),
            best_ask_price=Decimal(ticker["ask1Price"]),
            best_ask_size=Decimal(ticker["ask1Size"]),
            receive_latency_ms=(time.perf_counter() - started) * 1000.0,
            source_latency_ms=source_latency_ms,
        )

