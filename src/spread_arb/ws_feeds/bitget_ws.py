from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import WebSocketFeed


class BitgetWsFeed(WebSocketFeed):
    """Bitget v2 WebSocket — ticker channel (includes BBO data)."""

    ws_url = "wss://ws.bitget.com/v2/ws/public"
    ping_interval_sec = 25.0

    # Bitget uses 1000-prefix for low-price tokens (same as Binance/Bybit).
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
        return ExchangeName.BITGET

    def _to_bitget(self, symbol: Symbol) -> str:
        return self._SYMBOL_MAP.get(symbol, symbol)

    def _to_canonical(self, bitget_symbol: str) -> str:
        return self._REVERSE_MAP.get(bitget_symbol, bitget_symbol)

    def _build_subscribe_messages(self) -> list[str]:
        bitget_symbols = [self._to_bitget(s) for s in self.symbols]
        args = [
            {
                "instType": "USDT-FUTURES",
                "channel": "ticker",
                "instId": s,
            }
            for s in bitget_symbols
        ]
        messages = []
        batch_size = 30
        for i in range(0, len(args), batch_size):
            batch = args[i : i + batch_size]
            messages.append(json.dumps({"op": "subscribe", "args": batch}))
        return messages

    def _ping_payload(self) -> str:
        return "ping"

    def _is_pong(self, raw: str | bytes) -> bool:
        if isinstance(raw, str) and raw.strip() == "pong":
            return True
        return False

    def _parse_message(self, raw: str | bytes) -> Quote | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        data = json.loads(raw)

        # Skip subscription confirmations.
        if "event" in data:
            return None

        # Ticker updates: {"action":"snapshot", "arg":{...}, "data":[...]}
        arg = data.get("arg", {})
        if arg.get("channel") != "ticker":
            return None

        items = data.get("data", [])
        if not items:
            return None

        tick = items[0]
        inst_id = arg.get("instId", "") or tick.get("instId", "")
        canonical = self._to_canonical(inst_id)

        bid_price_raw = tick.get("bidPr") or tick.get("bestBid")
        bid_size_raw = tick.get("bidSz") or tick.get("bestBidSz")
        ask_price_raw = tick.get("askPr") or tick.get("bestAsk")
        ask_size_raw = tick.get("askSz") or tick.get("bestAskSz")

        if not bid_price_raw or not ask_price_raw:
            return None

        bid_price = Decimal(bid_price_raw)
        bid_size = Decimal(bid_size_raw) if bid_size_raw else Decimal(0)
        ask_price = Decimal(ask_price_raw)
        ask_size = Decimal(ask_size_raw) if ask_size_raw else Decimal(0)

        # Normalise 1000-prefix symbols.
        divisor = self._PRICE_DIVISOR.get(canonical)
        if divisor:
            d = Decimal(divisor)
            bid_price /= d
            ask_price /= d
            bid_size *= d
            ask_size *= d

        ts_ms = tick.get("ts")
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
