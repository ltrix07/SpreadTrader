from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import ExchangeClient


class HtxExchange(ExchangeClient):
    base_url = "https://api.hbdm.com"

    # HTX (Huobi) linear swap rate limit: 800 requests / minute.
    # With 20 symbols staggered at 80ms → cycle ~1.6s, fits safely.
    inter_request_delay_sec = 0.08

    # HTX uses dash-separated contract codes: BTC-USDT.
    # For low-price tokens, HTX does NOT use 1000 prefix.
    _STRIP_1000_PREFIX: dict[str, str] = {
        "1000BONKUSDT": "BONK-USDT",
    }

    _PRICE_MULTIPLIER: dict[str, int] = {
        "1000BONKUSDT": 1000,
    }

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.HTX

    @classmethod
    def _to_htx_contract(cls, symbol: Symbol) -> str:
        """Convert canonical symbol (e.g. BTCUSDT) to HTX contract_code (e.g. BTC-USDT)."""
        if symbol in cls._STRIP_1000_PREFIX:
            return cls._STRIP_1000_PREFIX[symbol]
        if symbol.endswith("USDT") and "_" not in symbol:
            base = symbol.removesuffix("USDT")
            return f"{base}-USDT"
        return symbol

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        contract_code = self._to_htx_contract(symbol)
        endpoint = f"{self.base_url}/linear-swap-ex/market/depth"
        params = {"contract_code": contract_code, "type": "step0"}

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        status = payload.get("status")
        if status != "ok":
            raise RuntimeError(f"HTX API error: {payload}")

        tick = payload.get("tick", {})
        bids = tick.get("bids", [])
        asks = tick.get("asks", [])
        if not bids or not asks:
            raise RuntimeError(f"HTX returned empty book for contract_code={contract_code}")

        # HTX depth format: [[price, size], ...]
        best_bid = bids[0]
        best_ask = asks[0]

        server_ms = payload.get("ts")
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = float(received_at_ms - server_ms) if isinstance(server_ms, int) else None

        bid_price = Decimal(str(best_bid[0]))
        bid_size = Decimal(str(best_bid[1]))
        ask_price = Decimal(str(best_ask[0]))
        ask_size = Decimal(str(best_ask[1]))

        # Normalise 1000-prefix symbols.
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
