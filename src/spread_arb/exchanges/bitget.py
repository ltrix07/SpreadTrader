from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import ExchangeClient


class BitgetExchange(ExchangeClient):
    base_url = "https://api.bitget.com"

    # Bitget public rate limit: 20 requests / second.
    # With 20 symbols staggered at 50ms → cycle ~1s, fits comfortably.
    inter_request_delay_sec = 0.05

    # Bitget USDT-M uses 1000-prefixed symbols for low-price tokens,
    # same convention as Bybit/Binance.
    _SYMBOL_MAP: dict[str, str] = {
        "PEPEUSDT": "1000PEPEUSDT",
        "FLOKIUSDT": "1000FLOKIUSDT",
    }

    _PRICE_DIVISOR: dict[str, int] = {
        "PEPEUSDT": 1000,
        "FLOKIUSDT": 1000,
    }

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.BITGET

    @classmethod
    def _to_bitget_symbol(cls, symbol: Symbol) -> str:
        return cls._SYMBOL_MAP.get(symbol, symbol)

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        bitget_symbol = self._to_bitget_symbol(symbol)
        endpoint = f"{self.base_url}/api/v2/mix/market/merge-depth"
        params = {
            "productType": "USDT-FUTURES",
            "symbol": bitget_symbol,
            "limit": "1",
        }

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        code = payload.get("code")
        if code != "00000":
            raise RuntimeError(f"Bitget API error: {payload}")

        data = payload.get("data", {})
        asks = data.get("asks", [])
        bids = data.get("bids", [])
        if not bids or not asks:
            raise RuntimeError(f"Bitget returned empty book for symbol={bitget_symbol}")

        # Bitget depth format: [[price, size], ...]
        best_bid = bids[0]
        best_ask = asks[0]

        server_ts = data.get("ts")
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = (
            float(received_at_ms - int(server_ts)) if server_ts else None
        )

        bid_price = Decimal(str(best_bid[0]))
        bid_size = Decimal(str(best_bid[1]))
        ask_price = Decimal(str(best_ask[0]))
        ask_size = Decimal(str(best_ask[1]))

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
