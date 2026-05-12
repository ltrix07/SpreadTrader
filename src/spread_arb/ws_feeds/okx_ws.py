from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import WebSocketFeed


class OkxWsFeed(WebSocketFeed):
    """OKX v5 WebSocket — bbo-tbt (tick-by-tick best bid/offer)."""

    ws_url = "wss://ws.okx.com:8443/ws/v5/public"
    ping_interval_sec = 25.0

    _STRIP_1000_PREFIX: dict[str, str] = {
        "1000BONKUSDT": "BONK",
    }
    _PRICE_MULTIPLIER: dict[str, int] = {
        "1000BONKUSDT": 1000,
    }

    # Reverse: OKX instId base → canonical symbol.
    _INST_TO_CANONICAL: dict[str, str] = {
        "BONK-USDT-SWAP": "1000BONKUSDT",
    }

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.OKX

    def _to_inst_id(self, symbol: Symbol) -> str:
        if symbol in self._STRIP_1000_PREFIX:
            base = self._STRIP_1000_PREFIX[symbol]
            return f"{base}-USDT-SWAP"
        if symbol.endswith("USDT") and "_" not in symbol:
            base = symbol.removesuffix("USDT")
            return f"{base}-USDT-SWAP"
        return symbol

    def _to_canonical(self, inst_id: str) -> str:
        if inst_id in self._INST_TO_CANONICAL:
            return self._INST_TO_CANONICAL[inst_id]
        # BTC-USDT-SWAP → BTCUSDT
        parts = inst_id.split("-")
        if len(parts) >= 2:
            return parts[0] + parts[1]
        return inst_id

    def _build_subscribe_messages(self) -> list[str]:
        args = [{"channel": "bbo-tbt", "instId": self._to_inst_id(s)} for s in self.symbols]
        # OKX allows many args per subscribe.
        messages = []
        batch_size = 50
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

        # Subscription confirmations and errors.
        if "event" in data:
            return None

        arg = data.get("arg", {})
        channel = arg.get("channel", "")
        if channel != "bbo-tbt":
            return None

        items = data.get("data", [])
        if not items:
            return None

        tick = items[0]
        inst_id = arg.get("instId", "")
        canonical = self._to_canonical(inst_id)

        bids = tick.get("bids", [])
        asks = tick.get("asks", [])
        if not bids or not asks:
            return None

        bid_price = Decimal(bids[0][0])
        bid_size = Decimal(bids[0][1])
        ask_price = Decimal(asks[0][0])
        ask_size = Decimal(asks[0][1])

        multiplier = self._PRICE_MULTIPLIER.get(canonical)
        if multiplier:
            m = Decimal(multiplier)
            bid_price *= m
            ask_price *= m
            bid_size /= m
            ask_size /= m

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
