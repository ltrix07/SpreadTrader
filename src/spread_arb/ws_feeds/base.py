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
    aiohttp_heartbeat_sec: float | None = 20.0
    receive_timeout_sec: float | None = None
    reconnect_base_sec: float = 1.0
    reconnect_max_sec: float = 30.0
    sample_message_limit: int = 3

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
        self._msg_count: int = 0
        self._quote_count: int = 0
        self._sample_logged_count: int = 0
        self._subscription_ok_count: int = 0
        self._subscription_error_count: int = 0
        self._server_ping_count: int = 0

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

    async def _handle_server_ping(
        self, ws: aiohttp.ClientWebSocketResponse, raw: str | bytes
    ) -> bool:
        """Handle server-initiated pings that need an application-level pong.

        Return True if the message was a server ping (and has been answered),
        so that the read loop can skip further processing.
        Subclasses override this for exchanges like Gate.io and HTX.
        """
        return False

    def _record_subscription_ok(self, detail: str | None = None, *, info_level: bool = False) -> None:
        self._subscription_ok_count += 1
        if detail:
            if info_level:
                self.log.info("subscription ok | %s", detail)
            else:
                self.log.debug("subscription ok | %s", detail)

    def _record_subscription_error(self, detail: str, payload: object | None = None) -> None:
        self._subscription_error_count += 1
        if payload is None:
            self.log.warning("subscription error | %s", detail)
        else:
            self.log.warning("subscription error | %s | payload=%s", detail, payload)

    def _effective_receive_timeout_sec(self) -> float:
        if self.receive_timeout_sec is not None:
            return self.receive_timeout_sec
        return max(self.ping_interval_sec * 3, 10.0)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop: connect -> subscribe -> read messages -> reconnect on error."""
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
            heartbeat=self.aiohttp_heartbeat_sec,
            receive_timeout=self._effective_receive_timeout_sec(),
        ) as ws:
            self._ws = ws
            self._msg_count = 0
            self._quote_count = 0
            self._sample_logged_count = 0
            self._subscription_ok_count = 0
            self._subscription_error_count = 0
            self._server_ping_count = 0
            self.log.info("connected, subscribing to %d symbols", len(self.symbols))

            # Send subscription messages.
            for msg in self._build_subscribe_messages():
                self.log.debug("subscribe payload: %s", msg)
                await ws.send_str(msg)
                # Small delay between subscribe batches to avoid rate limits.
                await asyncio.sleep(0.1)

            # Start ping task only when feed uses custom app-level pings.
            ping_task: asyncio.Task | None = None
            if self._ping_payload() is not None:
                ping_task = asyncio.create_task(
                    self._ping_loop(ws, stop_event),
                    name=f"ws-ping-{self.name.value}",
                )
            stats_task = asyncio.create_task(
                self._stats_loop(stop_event),
                name=f"ws-stats-{self.name.value}",
            )

            try:
                await self._read_loop(ws, stop_event)
            finally:
                if ping_task is not None:
                    ping_task.cancel()
                stats_task.cancel()
                if ping_task is not None:
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
                try:
                    await stats_task
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

            # Let subclasses handle server-initiated pings (Gate, HTX).
            try:
                if await self._handle_server_ping(ws, raw):
                    self._server_ping_count += 1
                    continue
            except Exception as exc:  # noqa: BLE001
                self.log.debug("server ping handler error: %s", exc)

            if self._is_pong(raw):
                continue

            self._msg_count += 1

            # Log first few non-ping/pong messages for startup diagnostics.
            if self._sample_logged_count < self.sample_message_limit:
                sample = str(raw)[:500] if isinstance(raw, str) else str(raw[:500])
                self._sample_logged_count += 1
                self.log.info("data message sample #%d: %s", self._sample_logged_count, sample)

            try:
                quote = self._parse_message(raw)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("parse error: %s | raw=%s", exc, str(raw)[:200])
                continue

            if quote is not None:
                self._quote_count += 1
                try:
                    callback_result = self.on_quote(quote)
                    if isinstance(callback_result, Awaitable):
                        await callback_result
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("on_quote callback error: %s", exc)

    async def _stats_loop(self, stop_event: asyncio.Event) -> None:
        """Periodically log message/quote counters for debugging."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30.0)
                return
            except TimeoutError:
                pass
            self.log.info(
                "ws stats | msgs=%d quotes=%d subs_ok=%d subs_err=%d server_pings=%d (%.1f%% parsed)",
                self._msg_count,
                self._quote_count,
                self._subscription_ok_count,
                self._subscription_error_count,
                self._server_ping_count,
                (self._quote_count / self._msg_count * 100) if self._msg_count else 0,
            )

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
