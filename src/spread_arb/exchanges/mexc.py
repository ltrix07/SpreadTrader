from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import ExchangeClient


class MexcExchange(ExchangeClient):
    base_url = "https://contract.mexc.com"
    inter_request_delay_sec = 0.12  # ~120ms between requests to stay under MEXC rate limit.

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.MEXC

    @staticmethod
    def _to_mexc_symbol(symbol: Symbol) -> str:
        if symbol.endswith("USDT") and "_" not in symbol:
            base = symbol.removesuffix("USDT")
            return f"{base}_USDT"
        return symbol

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        mexc_symbol = self._to_mexc_symbol(symbol)
        endpoint = f"{self.base_url}/api/v1/contract/depth/{mexc_symbol}"
        params = {"limit": 1}

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        if not payload.get("success"):
            raise RuntimeError(f"MEXC API error: {payload}")

        data = payload.get("data") or {}
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            raise RuntimeError(f"MEXC returned empty depth for symbol={symbol}")

        best_bid = bids[0]
        best_ask = asks[0]

        server_ms = data.get("timestamp")
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = float(received_at_ms - server_ms) if isinstance(server_ms, int) else None

        return Quote(
            received_at=received_at,
            exchange=self.name,
            symbol=symbol,
            best_bid_price=Decimal(str(best_bid[0])),
            best_bid_size=Decimal(str(best_bid[1])),
            best_ask_price=Decimal(str(best_ask[0])),
            best_ask_size=Decimal(str(best_ask[1])),
            receive_latency_ms=(time.perf_counter() - started) * 1000.0,
            source_latency_ms=source_latency_ms,
        )

