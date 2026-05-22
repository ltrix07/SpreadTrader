from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import WebSocketFeed


class BinanceWsFeed(WebSocketFeed):
    """Binance Futures WebSocket — bookTicker (real-time best bid/offer)."""

    ws_url = "wss://fstream.binance.com/ws"
    ping_interval_sec = 25.0

    # Binance Futures uses 1000-prefixed symbols for low-price tokens.
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
        return ExchangeName.BINANCE

    def _to_binance(self, symbol: Symbol) -> str:
        return self._SYMBOL_MAP.get(symbol, symbol)

    def _to_canonical(self, binance_symbol: str) -> str:
        return self._REVERSE_MAP.get(binance_symbol, binance_symbol)

    def _build_subscribe_messages(self) -> list[str]:
        # Binance allows subscribing to multiple streams in one message.
        binance_symbols = [self._to_binance(s) for s in self.symbols]
        params = [f"{s.lower()}@bookTicker" for s in binance_symbols]
        messages = []
        batch_size = 50
        for i in range(0, len(params), batch_size):
            batch = params[i : i + batch_size]
            messages.append(json.dumps({
                "method": "SUBSCRIBE",
                "params": batch,
                "id": i + 1,
            }))
        return messages

    def _build_subscribe_messages_for(self, symbols: list[str]) -> list[str]:
        binance_symbols = [self._to_binance(s) for s in symbols]
        params = [f"{s.lower()}@bookTicker" for s in binance_symbols]
        messages = []
        batch_size = 50
        for i in range(0, len(params), batch_size):
            batch = params[i : i + batch_size]
            messages.append(json.dumps({
                "method": "SUBSCRIBE",
                "params": batch,
                "id": 1000 + i,
            }))
        return messages

    def _build_unsubscribe_messages_for(self, symbols: list[str]) -> list[str]:
        binance_symbols = [self._to_binance(s) for s in symbols]
        params = [f"{s.lower()}@bookTicker" for s in binance_symbols]
        messages = []
        batch_size = 50
        for i in range(0, len(params), batch_size):
            batch = params[i : i + batch_size]
            messages.append(json.dumps({
                "method": "UNSUBSCRIBE",
                "params": batch,
                "id": 2000 + i,
            }))
        return messages

    def _ping_payload(self) -> str | bytes | None:
        # Binance WS uses native WS pings (aiohttp heartbeat handles it).
        return None

    def _is_pong(self, raw: str | bytes) -> bool:
        # Subscription confirmations have "result": null and "id".
        if isinstance(raw, str) and '"result"' in raw:
            return True
        return False

    def _parse_message(self, raw: str | bytes) -> Quote | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        data = json.loads(raw)

        # bookTicker events have "e": "bookTicker".
        if data.get("e") != "bookTicker":
            return None

        binance_symbol = data.get("s", "")
        canonical = self._to_canonical(binance_symbol)

        bid_price = Decimal(data["b"])
        bid_size = Decimal(data["B"])
        ask_price = Decimal(data["a"])
        ask_size = Decimal(data["A"])

        # Normalise 1000-prefix symbols.
        divisor = self._PRICE_DIVISOR.get(canonical)
        if divisor:
            d = Decimal(divisor)
            bid_price /= d
            ask_price /= d
            bid_size *= d
            ask_size *= d

        ts_ms = data.get("T")
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
            receive_latency_ms=0.0,
            source_latency_ms=source_latency_ms,
        )
