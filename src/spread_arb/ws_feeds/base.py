from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence

import aiohttp

from ..models import ExchangeName, Quote, Symbol

QuoteCallback = Callable[[Quote], Awaitable[None] | None]


class WebSocketFeed(ABC):
    """Base class for exchange WebSocket BBO feeds.

    Each subclass connects to one WS endpoint, subscribes to BBO / bookTicker
    for all requested symbols, and pushes Quote objects via the callback.
    """

    # Subclasses must set these.
    ws_url: str = ""
    ping_interval_sec: float = 20.0
    reconnect_base_sec: float = 1.0
    reconnect_max_sec: float = 30.0

    def __init__(
        self,
        session: aiohttp.ClientSession,
        symbols: Sequence[Symbol],
        on_quote: QuoteCallback,
    ) -> None:
        self.session = session
        self.symbols = list(symbols)
        self.on_quote = on_quote
        self.log = logging.getLogger(f"{__name__}.{self.name.value}")
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    @property
    @abstractmethod
    def name(self) -> ExchangeName:
        raise NotImplementedError

    @abstractmethod
    def _build_subscribe_messages(self) -> list[str]:
        """Return JSON strings to send after WS connect to subscribe to BBO."""
        raise NotImplementedError

    @abstractmethod
    def _parse_message(self, raw: str | bytes) -> Quote | None:
        """Parse an incoming WS message into a Quote, or None if irrelevant."""
        raise NotImplementedError

    def _ping_payload(self) -> str | bytes | None:
        """Return the ping payload to send. None = use WS-level ping."""
        return json.dumps({"op": "ping"})

    def _is_pong(self, raw: str | bytes) -> bool:
        """Return True if this message is a pong response (skip parsing)."""
        if isinstance(raw, str) and "pong" in raw.lower():
            return True
        return False

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop: connect → subscribe → read messages → reconnect on error."""
        backoff = self.reconnect_base_sec

        while not stop_event.is_set():
            try:
                await self._connect_and_listen(stop_event)
                backoff = self.reconnect_base_sec  # reset on clean disconnect
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning("ws error, reconnecting in %.1fs: %s", backoff, exc)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                    return  # stop_event was set
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, self.reconnect_max_sec)

    async def _connect_and_listen(self, stop_event: asyncio.Event) -> None:
        self.log.info("connecting to %s", self.ws_url)

        async with self.session.ws_connect(
            self.ws_url,
            heartbeat=self.ping_interval_sec,
            receive_timeout=self.ping_interval_sec * 3,
        ) as ws:
            self._ws = ws
            self.log.info("connected, subscribing to %d symbols", len(self.symbols))

            # Send subscription messages.
            for msg in self._build_subscribe_messages():
                await ws.send_str(msg)
                # Small delay between subscribe batches to avoid rate limits.
                await asyncio.sleep(0.1)

            # Start ping task.
            ping_task = asyncio.create_task(
                self._ping_loop(ws, stop_event),
                name=f"ws-ping-{self.name.value}",
            )

            try:
                await self._read_loop(ws, stop_event)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                self._ws = None

    async def _read_loop(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        stop_event: asyncio.Event,
    ) -> None:
        async for msg in ws:
            if stop_event.is_set():
                break

            if msg.type == aiohttp.WSMsgType.TEXT:
                raw = msg.data
            elif msg.type == aiohttp.WSMsgType.BINARY:
                raw = msg.data
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                self.log.info("ws closed by server")
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                self.log.warning("ws error: %s", ws.exception())
                break
            else:
                continue

            if self._is_pong(raw):
                continue

            try:
                quote = self._parse_message(raw)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("parse error: %s | raw=%s", exc, str(raw)[:200])
                continue

            if quote is not None:
                try:
                    callback_result = self.on_quote(quote)
                    if isinstance(callback_result, Awaitable):
                        await callback_result
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("on_quote callback error: %s", exc)

    async def _ping_loop(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set() and not ws.closed:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.ping_interval_sec)
                return  # stop_event set
            except TimeoutError:
                pass

            payload = self._ping_payload()
            if payload is not None:
                try:
                    if isinstance(payload, bytes):
                        await ws.send_bytes(payload)
                    else:
                        await ws.send_str(payload)
                except Exception:  # noqa: BLE001
                    break  # connection lost, will reconnect
