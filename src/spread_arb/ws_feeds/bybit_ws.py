from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import WebSocketFeed


class BybitWsFeed(WebSocketFeed):
    """Bybit v5 linear WebSocket — orderbook.1 (level-1 BBO snapshots)."""

    ws_url = "wss://stream.bybit.com/v5/public/linear"
    ping_interval_sec = 20.0

    # Same mappings as REST client.
    _SYMBOL_MAP: dict[str, str] = {
        "PEPEUSDT": "1000PEPEUSDT",
        "FLOKIUSDT": "1000FLOKIUSDT",
    }
    _REVERSE_MAP: dict[str, str] = {v: k for k, v in _SYMBOL_MAP.items()}

    _PRICE_DIVISOR: dict[str, int] = {
        "PEPEUSDT": 1000,
        "FLOKIUSDT": 1000,
    }

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.BYBIT

    def _to_bybit(self, symbol: Symbol) -> str:
        return self._SYMBOL_MAP.get(symbol, symbol)

    def _to_canonical(self, bybit_symbol: str) -> str:
        return self._REVERSE_MAP.get(bybit_symbol, bybit_symbol)

    def _build_subscribe_messages(self) -> list[str]:
        # Bybit supports up to 10 args per subscribe message.
        bybit_symbols = [self._to_bybit(s) for s in self.symbols]
        args = [f"orderbook.1.{s}" for s in bybit_symbols]
        messages = []
        batch_size = 10
        for i in range(0, len(args), batch_size):
            batch = args[i : i + batch_size]
            messages.append(json.dumps({"op": "subscribe", "args": batch}))
        return messages

    def _ping_payload(self) -> str:
        return json.dumps({"op": "ping"})

    def _is_pong(self, raw: str | bytes) -> bool:
        if isinstance(raw, str) and '"pong"' in raw:
            return True
        return False

    def _parse_message(self, raw: str | bytes) -> Quote | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        data = json.loads(raw)
        topic = data.get("topic", "")
        if not topic.startswith("orderbook.1."):
            return None

        book_data = data.get("data", {})
        bids = book_data.get("b", [])
        asks = book_data.get("a", [])
        if not bids or not asks:
            return None

        bybit_symbol = book_data.get("s", topic.split(".")[-1])
        canonical = self._to_canonical(bybit_symbol)

        bid_price = Decimal(bids[0][0])
        bid_size = Decimal(bids[0][1])
        ask_price = Decimal(asks[0][0])
        ask_size = Decimal(asks[0][1])

        divisor = self._PRICE_DIVISOR.get(canonical)
        if divisor:
            d = Decimal(divisor)
            bid_price /= d
            ask_price /= d
            bid_size *= d
            ask_size *= d

        ts_ms = data.get("ts")
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = float(received_at_ms - int(ts_ms)) if ts_ms else None

        return Quote(
            received_at=received_at,
            exchange=self.name,
            symbol=canonical,
            best_bid_price=bid_price,
            best_bid_size=bid_size,
            best_ask_price=ask_price,
            best_ask_size=ask_size,
            receive_latency_ms=0.0,  # WS — negligible.
            source_latency_ms=source_latency_ms,
        )
