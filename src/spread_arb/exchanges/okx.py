from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import ExchangeClient


class OkxExchange(ExchangeClient):
    base_url = "https://www.okx.com"

    # OKX public rate limit: 20 requests per 2 seconds.
    # With 20 symbols staggered at 100ms each → cycle ~2s, fits the limit.
    inter_request_delay_sec = 0.10

    # Canonical symbols that use a "1000" prefix but OKX quotes without it.
    # e.g. canonical "1000BONKUSDT" → OKX "BONK-USDT-SWAP".
    # We must multiply OKX price by 1000 and divide size by 1000 to normalise.
    _STRIP_1000_PREFIX: dict[str, str] = {
        "1000BONKUSDT": "BONK",
    }

    # Inverse of Bybit's _PRICE_DIVISOR: when OKX quotes per-unit but
    # canonical is per-1000-units, multiply price by this factor.
    _PRICE_MULTIPLIER: dict[str, int] = {
        "1000BONKUSDT": 1000,
    }

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.OKX

    @classmethod
    def _to_okx_inst_id(cls, symbol: Symbol) -> str:
        """Convert canonical symbol (e.g. BTCUSDT) to OKX instId (e.g. BTC-USDT-SWAP)."""
        # Handle 1000-prefix symbols first.
        if symbol in cls._STRIP_1000_PREFIX:
            base = cls._STRIP_1000_PREFIX[symbol]
            return f"{base}-USDT-SWAP"
        if symbol.endswith("USDT") and "_" not in symbol:
            base = symbol.removesuffix("USDT")
            return f"{base}-USDT-SWAP"
        return symbol

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        inst_id = self._to_okx_inst_id(symbol)
        endpoint = f"{self.base_url}/api/v5/market/books"
        params = {"instId": inst_id, "sz": "1"}

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        code = payload.get("code")
        if code != "0":
            raise RuntimeError(f"OKX API error: {payload}")

        data_list = payload.get("data", [])
        if not data_list:
            raise RuntimeError(f"OKX returned no data for instId={inst_id}")

        book = data_list[0]
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            raise RuntimeError(f"OKX returned empty book for instId={inst_id}")

        # OKX orderbook format: [price, size, deprecated, numOrders]
        best_bid = bids[0]
        best_ask = asks[0]

        server_ts = book.get("ts")
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = (
            float(received_at_ms - int(server_ts)) if server_ts else None
        )

        bid_price = Decimal(str(best_bid[0]))
        bid_size = Decimal(str(best_bid[1]))
        ask_price = Decimal(str(best_ask[0]))
        ask_size = Decimal(str(best_ask[1]))

        # Normalise when OKX quotes per-unit but canonical is per-1000-units.
        # e.g. OKX quotes BONK at 0.000015 but canonical 1000BONKUSDT = 0.015.
        multiplier = self._PRICE_MULTIPLIER.get(symbol)
        if multiplier:
            m = Decimal(multiplier)
            bid_price *= m
            ask_price *= m
            bid_size /= m
            ask_size /= m

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
