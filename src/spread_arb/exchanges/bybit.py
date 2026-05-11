from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import ExchangeClient


class BybitExchange(ExchangeClient):
    base_url = "https://api.bybit.com"

    # Canonical (unified) symbol → Bybit-specific symbol.
    # Bybit uses 1000-prefixed symbols for low-price tokens and drops
    # the prefix for others where MEXC keeps it.
    _SYMBOL_MAP: dict[str, str] = {
        "PEPEUSDT": "1000PEPEUSDT",
        "FLOKIUSDT": "1000FLOKIUSDT",
    }

    # When Bybit quotes a "1000X" contract but our canonical symbol is "X",
    # we must divide price by 1000 and multiply size by 1000 to normalise.
    _PRICE_DIVISOR: dict[str, int] = {
        "PEPEUSDT": 1000,      # Bybit 1000PEPE → canonical PEPE
        "FLOKIUSDT": 1000,     # Bybit 1000FLOKI → canonical FLOKI
    }

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.BYBIT

    @classmethod
    def _to_bybit_symbol(cls, symbol: Symbol) -> str:
        return cls._SYMBOL_MAP.get(symbol, symbol)

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        bybit_symbol = self._to_bybit_symbol(symbol)
        endpoint = f"{self.base_url}/v5/market/tickers"
        params = {"category": "linear", "symbol": bybit_symbol}

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

        bid_price = Decimal(ticker["bid1Price"])
        bid_size = Decimal(ticker["bid1Size"])
        ask_price = Decimal(ticker["ask1Price"])
        ask_size = Decimal(ticker["ask1Size"])

        # Normalise prices/sizes when Bybit contract unit differs from canonical.
        # e.g. Bybit quotes 1000PEPEUSDT but we track PEPEUSDT → divide price by 1000.
        divisor = self._PRICE_DIVISOR.get(symbol)
        if divisor:
            d = Decimal(divisor)
            bid_price /= d
            ask_price /= d
            bid_size *= d
            ask_size *= d

        return Quote(
            received_at=received_at,
            exchange=self.name,
            symbol=symbol,
            best_bid_price=bid_price,
            best_bid_size=bid_size,
            best_ask_price=ask_price,
            best_ask_size=ask_size,
            receive_latency_ms=(time.perf_counter() - started) * 1000.0,
            source_latency_ms=source_latency_ms,
        )

