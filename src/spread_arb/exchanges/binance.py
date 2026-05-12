from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import ExchangeClient


class BinanceExchange(ExchangeClient):
    base_url = "https://fapi.binance.com"

    # Binance Futures public rate limit: 2400 request weight / minute.
    # depth?limit=5 costs 2 weight → 20 symbols × 2 = 40 per cycle — very comfortable.
    # No stagger needed.
    inter_request_delay_sec = 0.0

    # Binance Futures uses 1000-prefixed symbols for low-price tokens.
    _SYMBOL_MAP: dict[str, str] = {
        "PEPEUSDT": "1000PEPEUSDT",
        "FLOKIUSDT": "1000FLOKIUSDT",
    }

    # When Binance quotes a "1000X" contract but our canonical symbol is "X",
    # we must divide price by 1000 and multiply size by 1000.
    _PRICE_DIVISOR: dict[str, int] = {
        "PEPEUSDT": 1000,
        "FLOKIUSDT": 1000,
    }

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.BINANCE

    @classmethod
    def _to_binance_symbol(cls, symbol: Symbol) -> str:
        return cls._SYMBOL_MAP.get(symbol, symbol)

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        binance_symbol = self._to_binance_symbol(symbol)
        endpoint = f"{self.base_url}/fapi/v1/depth"
        params = {"symbol": binance_symbol, "limit": "5"}

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        bids = payload.get("bids", [])
        asks = payload.get("asks", [])
        if not bids or not asks:
            raise RuntimeError(f"Binance returned empty book for symbol={binance_symbol}")

        # Binance depth format: [[price, qty], ...]
        best_bid = bids[0]
        best_ask = asks[0]

        server_ms = payload.get("T")  # Transaction time
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = float(received_at_ms - server_ms) if isinstance(server_ms, int) else None

        bid_price = Decimal(best_bid[0])
        bid_size = Decimal(best_bid[1])
        ask_price = Decimal(best_ask[0])
        ask_size = Decimal(best_ask[1])

        # Normalise 1000-prefix symbols.
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
