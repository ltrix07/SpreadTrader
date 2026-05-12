from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from decimal import Decimal

from ..models import ExchangeName, Quote, Symbol
from .base import WebSocketFeed


class HtxWsFeed(WebSocketFeed):
    """HTX (Huobi) linear-swap WebSocket — market.{symbol}.bbo.

    HTX sends ALL messages gzip-compressed as binary frames.
    The server sends {"ping": ts} and expects {"pong": ts} back.
    """

    ws_url = "wss://api.hbdm.com/linear-swap-ws"
    ping_interval_sec = 20.0

    # HTX uses dash-separated, no 1000-prefix for BONK.
    _STRIP_1000_PREFIX: dict[str, str] = {
        "1000BONKUSDT": "BONK-USDT",
    }
    _PRICE_MULTIPLIER: dict[str, int] = {
        "1000BONKUSDT": 1000,
    }

    # Reverse: HTX contract → canonical symbol.
    _CONTRACT_TO_CANONICAL: dict[str, str] = {
        "BONK-USDT": "1000BONKUSDT",
    }

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.HTX

    def _to_htx_contract(self, symbol: Symbol) -> str:
        if symbol in self._STRIP_1000_PREFIX:
            return self._STRIP_1000_PREFIX[symbol]
        if symbol.endswith("USDT") and "_" not in symbol:
            base = symbol.removesuffix("USDT")
            return f"{base}-USDT"
        return symbol

    def _to_canonical(self, contract: str) -> str:
        if contract in self._CONTRACT_TO_CANONICAL:
            return self._CONTRACT_TO_CANONICAL[contract]
        # BTC-USDT → BTCUSDT
        return contract.replace("-", "")

    def _build_subscribe_messages(self) -> list[str]:
        # HTX requires one subscribe per topic.
        messages = []
        for s in self.symbols:
            contract = self._to_htx_contract(s)
            topic = f"market.{contract}.bbo"
            messages.append(json.dumps({"sub": topic, "id": f"bbo_{contract}"}))
        return messages

    def _ping_payload(self) -> str | bytes | None:
        # HTX server sends pings; we respond in _parse_message.
        # No need for our own ping loop — return None.
        return None

    def _is_pong(self, raw: str | bytes) -> bool:
        # We handle server pings inside _parse_message, so nothing here.
        return False

    def _decompress(self, raw: str | bytes) -> str:
        """HTX sends gzip-compressed binary frames."""
        if isinstance(raw, bytes):
            try:
                return gzip.decompress(raw).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                return raw.decode("utf-8", errors="replace")
        return raw

    def _parse_message(self, raw: str | bytes) -> Quote | None:
        text = self._decompress(raw)
        data = json.loads(text)

        # HTX server ping — must respond with pong immediately.
        if "ping" in data:
            import asyncio
            ts = data["ping"]
            pong = json.dumps({"pong": ts})
            if self._ws is not None and not self._ws.closed:
                asyncio.ensure_future(self._ws.send_str(pong))
            return None

        # Subscription confirmations.
        if "subbed" in data:
            return None

        # BBO update: {"ch": "market.BTC-USDT.bbo", "ts": ..., "tick": {...}}
        ch = data.get("ch", "")
        if ".bbo" not in ch:
            return None

        tick = data.get("tick", {})
        if not tick:
            return None

        # Extract contract code from channel: market.{contract}.bbo
        parts = ch.split(".")
        if len(parts) >= 3:
            contract = parts[1]
        else:
            return None

        canonical = self._to_canonical(contract)

        bid_list = tick.get("bid", [])
        ask_list = tick.get("ask", [])
        if not bid_list or not ask_list:
            return None

        # HTX BBO format: "bid": [price, size], "ask": [price, size]
        bid_price = Decimal(str(bid_list[0]))
        bid_size = Decimal(str(bid_list[1]))
        ask_price = Decimal(str(ask_list[0]))
        ask_size = Decimal(str(ask_list[1]))

        # Normalise 1000-prefix symbols.
        multiplier = self._PRICE_MULTIPLIER.get(canonical)
        if multiplier:
            m = Decimal(multiplier)
            bid_price *= m
            ask_price *= m
            bid_size /= m
            ask_size /= m

        ts_ms = data.get("ts") or tick.get("ts")
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
