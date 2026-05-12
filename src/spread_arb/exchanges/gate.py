from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import ExchangeClient


class GateExchange(ExchangeClient):
    base_url = "https://api.gateio.ws"

    # Gate.io public rate limit: 900 requests / minute (15 req/s).
    # With 20 symbols staggered at 70ms → cycle ~1.4s, well within limits.
    inter_request_delay_sec = 0.07

    # Gate.io uses underscore-separated contract names: BTC_USDT.
    # For low-price tokens, Gate uses the raw name (no 1000 prefix).
    _STRIP_1000_PREFIX: dict[str, str] = {
        "1000BONKUSDT": "BONK_USDT",
    }

    # When Gate quotes per-unit but canonical is per-1000-units,
    # multiply price by this factor.
    _PRICE_MULTIPLIER: dict[str, int] = {
        "1000BONKUSDT": 1000,
    }

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.GATE

    @classmethod
    def _to_gate_contract(cls, symbol: Symbol) -> str:
        """Convert canonical symbol (e.g. BTCUSDT) to Gate contract (e.g. BTC_USDT)."""
        if symbol in cls._STRIP_1000_PREFIX:
            return cls._STRIP_1000_PREFIX[symbol]
        if symbol.endswith("USDT") and "_" not in symbol:
            base = symbol.removesuffix("USDT")
            return f"{base}_USDT"
        return symbol

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        contract = self._to_gate_contract(symbol)
        endpoint = f"{self.base_url}/api/v4/futures/usdt/order_book"
        params = {"contract": contract, "limit": "1", "with_id": "true"}

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        # Gate returns {"asks": [{"p":"...", "s":...}], "bids": [...], "update_id":...}
        asks = payload.get("asks", [])
        bids = payload.get("bids", [])
        if not bids or not asks:
            raise RuntimeError(f"Gate returned empty book for contract={contract}")

        best_bid = bids[0]
        best_ask = asks[0]

        received_at = datetime.now(UTC)

        bid_price = Decimal(str(best_bid["p"]))
        bid_size = Decimal(str(best_bid["s"]))
        ask_price = Decimal(str(best_ask["p"]))
        ask_size = Decimal(str(best_ask["s"]))

        # Gate sizes are in contracts; for most USDT pairs 1 contract = 1 unit.
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
            source_latency_ms=None,  # Gate doesn't include server timestamp in orderbook.
        )
