from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal

import aiohttp

from ..models import ExchangeName, Quote, Symbol
from .base import WebSocketFeed


class GateWsFeed(WebSocketFeed):
    """Gate.io Futures WebSocket — futures.book_ticker (best bid/offer)."""

    ws_url = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    ping_interval_sec = 15.0
    aiohttp_heartbeat_sec = None
    subscribe_batch_size = 10

    # Gate uses underscore-separated, no 1000-prefix for BONK.
    _STRIP_1000_PREFIX: dict[str, str] = {
        "1000BONKUSDT": "BONK_USDT",
    }
    _PRICE_MULTIPLIER: dict[str, int] = {
        "1000BONKUSDT": 1000,
    }

    # Reverse: Gate contract → canonical symbol.
    _CONTRACT_TO_CANONICAL: dict[str, str] = {
        "BONK_USDT": "1000BONKUSDT",
    }

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.GATE

    def _to_gate_contract(self, symbol: Symbol) -> str:
        if symbol in self._STRIP_1000_PREFIX:
            return self._STRIP_1000_PREFIX[symbol]
        if symbol.endswith("USDT") and "_" not in symbol:
            base = symbol.removesuffix("USDT")
            return f"{base}_USDT"
        return symbol

    def _to_canonical(self, contract: str) -> str:
        if contract in self._CONTRACT_TO_CANONICAL:
            return self._CONTRACT_TO_CANONICAL[contract]
        # BTC_USDT → BTCUSDT
        return contract.replace("_", "")

    def _build_subscribe_messages(self) -> list[str]:
        contracts = [self._to_gate_contract(s) for s in self.symbols]
        # Gate allows subscribing to multiple contracts per message.
        messages = []
        batch_size = self.subscribe_batch_size
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i : i + batch_size]
            messages.append(json.dumps({
                "time": int(time.time()),
                "channel": "futures.book_ticker",
                "event": "subscribe",
                "payload": batch,
            }))
        return messages

    def _build_subscribe_messages_for(self, symbols: list[str]) -> list[str]:
        contracts = [self._to_gate_contract(s) for s in symbols]
        messages = []
        batch_size = self.subscribe_batch_size
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i : i + batch_size]
            messages.append(json.dumps({
                "time": int(time.time()),
                "channel": "futures.book_ticker",
                "event": "subscribe",
                "payload": batch,
            }))
        return messages

    def _build_unsubscribe_messages_for(self, symbols: list[str]) -> list[str]:
        contracts = [self._to_gate_contract(s) for s in symbols]
        messages = []
        batch_size = self.subscribe_batch_size
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i : i + batch_size]
            messages.append(json.dumps({
                "time": int(time.time()),
                "channel": "futures.book_ticker",
                "event": "unsubscribe",
                "payload": batch,
            }))
        return messages

    def _ping_payload(self) -> str:
        return json.dumps({
            "time": int(time.time()),
            "channel": "futures.ping",
        })

    async def _handle_server_ping(
        self, ws: aiohttp.ClientWebSocketResponse, raw: str | bytes
    ) -> bool:
        """Gate.io sends {"channel": "futures.ping"} -- must respond with pong."""
        if isinstance(raw, bytes):
            return False
        if "futures.ping" not in raw:
            return False
        pong = json.dumps({
            "time": int(time.time()),
            "channel": "futures.pong",
        })
        await ws.send_str(pong)
        return True

    def _is_pong(self, raw: str | bytes) -> bool:
        if isinstance(raw, str) and "futures.pong" in raw:
            return True
        return False

    def _parse_message(self, raw: str | bytes) -> Quote | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        data = json.loads(raw)

        # Subscription confirmation/error diagnostics.
        channel = data.get("channel", "")
        event = data.get("event", "")
        if channel == "futures.book_ticker" and event == "subscribe":
            error = data.get("error")
            status = data.get("status")
            result = data.get("result")
            if error is not None or status == "error":
                self._record_subscription_error(
                    (
                        f"channel={channel} event={event} "
                        f"status={status!r} error={error!r} result={result!r}"
                    ),
                    payload=data,
                )
            else:
                self._record_subscription_ok(
                    f"channel={channel} event={event} status={status!r} result={result!r}",
                )
            return None

        # Only process book_ticker update events.
        if channel != "futures.book_ticker" or event != "update":
            return None

        result = data.get("result", {})
        contract = result.get("s", "")
        canonical = self._to_canonical(contract)

        bid_price_raw = result.get("b", "")
        ask_price_raw = result.get("a", "")
        bid_size_raw = result.get("B", "")
        ask_size_raw = result.get("A", "")

        if not bid_price_raw or not ask_price_raw:
            return None

        bid_price = Decimal(bid_price_raw)
        bid_size = Decimal(str(bid_size_raw)) if bid_size_raw else Decimal(0)
        ask_price = Decimal(ask_price_raw)
        ask_size = Decimal(str(ask_size_raw)) if ask_size_raw else Decimal(0)

        # Normalise 1000-prefix symbols.
        multiplier = self._PRICE_MULTIPLIER.get(canonical)
        if multiplier:
            m = Decimal(multiplier)
            bid_price *= m
            ask_price *= m
            bid_size /= m
            ask_size /= m

        ts_ms = result.get("t")
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
