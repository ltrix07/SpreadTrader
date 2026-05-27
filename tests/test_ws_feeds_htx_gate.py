import asyncio
import gzip
import json
from collections.abc import Awaitable
from decimal import Decimal
from typing import cast

import aiohttp

from spread_arb.models import ExchangeName, Quote
from spread_arb.ws_feeds.base import WebSocketFeed
from spread_arb.ws_feeds.gate_ws import GateWsFeed
from spread_arb.ws_feeds.htx_ws import HtxWsFeed
from spread_arb.ws_feeds.okx_ws import OkxWsFeed


def _noop_quote(_: Quote) -> Awaitable[None] | None:
    return None


def _make_htx_feed() -> HtxWsFeed:
    return HtxWsFeed(
        session=cast(aiohttp.ClientSession, object()),
        symbols=["SOLUSDT"],
        on_quote=_noop_quote,
    )


def _make_gate_feed() -> GateWsFeed:
    return GateWsFeed(
        session=cast(aiohttp.ClientSession, object()),
        symbols=["SOLUSDT"],
        on_quote=_noop_quote,
    )


def _make_okx_feed() -> OkxWsFeed:
    return OkxWsFeed(
        session=cast(aiohttp.ClientSession, object()),
        symbols=["SOLUSDT"],
        on_quote=_noop_quote,
    )


def _gzip_json(payload: dict[str, object]) -> bytes:
    return gzip.compress(json.dumps(payload).encode("utf-8"))


def test_htx_parse_subscription_ok_increments_counter() -> None:
    feed = _make_htx_feed()
    raw = _gzip_json({"status": "ok", "subbed": "market.SOL-USDT.bbo", "id": "bbo_SOL-USDT"})

    quote = feed._parse_message(raw)

    assert quote is None
    assert feed._subscription_ok_count == 1
    assert feed._subscription_error_count == 0


def test_htx_parse_subscription_error_increments_counter() -> None:
    feed = _make_htx_feed()
    raw = _gzip_json({
        "status": "error",
        "err-code": "bad-request",
        "err-msg": "unknown topic",
        "subbed": "market.BAD-USDT.bbo",
    })

    quote = feed._parse_message(raw)

    assert quote is None
    assert feed._subscription_ok_count == 0
    assert feed._subscription_error_count == 1


def test_gate_parse_subscription_ok_increments_counter() -> None:
    feed = _make_gate_feed()
    raw = json.dumps({
        "channel": "futures.book_ticker",
        "event": "subscribe",
        "status": "success",
        "result": {"status": "success"},
    })

    quote = feed._parse_message(raw)

    assert quote is None
    assert feed._subscription_ok_count == 1
    assert feed._subscription_error_count == 0


def test_gate_parse_subscription_error_increments_counter() -> None:
    feed = _make_gate_feed()
    raw = json.dumps({
        "channel": "futures.book_ticker",
        "event": "subscribe",
        "error": {"code": 2, "message": "invalid contract"},
    })

    quote = feed._parse_message(raw)

    assert quote is None
    assert feed._subscription_ok_count == 0
    assert feed._subscription_error_count == 1


def test_gate_parse_update_normalizes_contract_sizes_to_base_units() -> None:
    feed = GateWsFeed(
        session=cast(aiohttp.ClientSession, object()),
        symbols=["SOLUSDT"],
        on_quote=_noop_quote,
        base_qty_per_contract={"SOLUSDT": Decimal("0.001")},
    )
    raw = json.dumps({
        "channel": "futures.book_ticker",
        "event": "update",
        "result": {
            "s": "SOL_USDT",
            "b": "100.0",
            "B": "500",
            "a": "100.1",
            "A": "750",
            "t": "1712345678901",
        },
    })

    quote = feed._parse_message(raw)

    assert quote is not None
    assert quote.best_bid_size == Decimal("0.500")
    assert quote.best_ask_size == Decimal("0.750")


def test_okx_parse_update_normalizes_contract_sizes_to_base_units() -> None:
    feed = OkxWsFeed(
        session=cast(aiohttp.ClientSession, object()),
        symbols=["SOLUSDT"],
        on_quote=_noop_quote,
        base_qty_per_contract={"SOLUSDT": Decimal("0.1")},
    )
    raw = json.dumps({
        "arg": {"channel": "bbo-tbt", "instId": "SOL-USDT-SWAP"},
        "data": [{
            "bids": [["100.0", "5", "0", "1"]],
            "asks": [["100.1", "7", "0", "1"]],
            "ts": "1712345678901",
        }],
    })

    quote = feed._parse_message(raw)

    assert quote is not None
    assert quote.best_bid_size == Decimal("0.5")
    assert quote.best_ask_size == Decimal("0.7")


def test_htx_server_ping_is_handled_and_pong_is_sent() -> None:
    feed = _make_htx_feed()
    raw = _gzip_json({"ping": 1712345678901})

    class DummyWs:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_str(self, payload: str) -> None:
            self.sent.append(payload)

    ws = DummyWs()

    handled = asyncio.run(feed._handle_server_ping(cast(aiohttp.ClientWebSocketResponse, ws), raw))

    assert handled is True
    assert ws.sent == ['{"pong": 1712345678901}']


def test_ping_payload_none_disables_ping_task_start() -> None:
    class DummyWs:
        closed = False

        async def send_str(self, _: str) -> None:
            return None

    class DummyWsCtx:
        def __init__(self, ws: DummyWs) -> None:
            self.ws = ws

        async def __aenter__(self) -> DummyWs:
            return self.ws

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class DummySession:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.ws = DummyWs()

        def ws_connect(self, *_args, **kwargs):
            self.calls.append(kwargs)
            return DummyWsCtx(self.ws)

    class NoPingFeed(WebSocketFeed):
        ws_url = "wss://example.com/ws"
        ping_interval_sec = 5.0
        aiohttp_heartbeat_sec = None

        def __init__(self, session: DummySession) -> None:
            super().__init__(session=cast(aiohttp.ClientSession, session), symbols=["SOLUSDT"], on_quote=_noop_quote)
            self.ping_loop_started = False

        @property
        def name(self) -> ExchangeName:
            return ExchangeName.HTX

        def _build_subscribe_messages(self) -> list[str]:
            return []

        def _parse_message(self, raw: str | bytes) -> Quote | None:
            return None

        def _ping_payload(self) -> str | bytes | None:
            return None

        async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse, stop_event: asyncio.Event) -> None:
            stop_event.set()

        async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse, stop_event: asyncio.Event) -> None:
            self.ping_loop_started = True
            await super()._ping_loop(ws, stop_event)

    async def _run_test() -> tuple[NoPingFeed, DummySession]:
        stop_event = asyncio.Event()
        session = DummySession()
        feed = NoPingFeed(session=session)
        await feed._connect_and_listen(stop_event)
        return feed, session

    feed, session = asyncio.run(_run_test())

    assert feed.ping_loop_started is False
    assert len(session.calls) == 1
    assert session.calls[0]["heartbeat"] is None


def test_subscribe_symbols_updates_durable_state_when_socket_is_unavailable() -> None:
    class DummyFeed(WebSocketFeed):
        ws_url = "wss://example.com/ws"

        @property
        def name(self) -> ExchangeName:
            return ExchangeName.HTX

        def _build_subscribe_messages(self) -> list[str]:
            return []

        def _parse_message(self, raw: str | bytes) -> Quote | None:
            return None

    async def _run() -> None:
        feed = DummyFeed(
            session=cast(aiohttp.ClientSession, object()),
            symbols=["SOLUSDT"],
            on_quote=_noop_quote,
        )
        feed._ws = None

        await feed.subscribe_symbols(["BTCUSDT", "SOLUSDT"])

        assert feed.symbols == ["SOLUSDT", "BTCUSDT"]

    asyncio.run(_run())


def test_unsubscribe_symbols_updates_durable_state_when_socket_is_unavailable() -> None:
    class DummyFeed(WebSocketFeed):
        ws_url = "wss://example.com/ws"

        @property
        def name(self) -> ExchangeName:
            return ExchangeName.HTX

        def _build_subscribe_messages(self) -> list[str]:
            return []

        def _parse_message(self, raw: str | bytes) -> Quote | None:
            return None

    async def _run() -> None:
        feed = DummyFeed(
            session=cast(aiohttp.ClientSession, object()),
            symbols=["SOLUSDT", "BTCUSDT"],
            on_quote=_noop_quote,
        )
        feed._ws = None

        await feed.unsubscribe_symbols(["BTCUSDT"])

        assert feed.symbols == ["SOLUSDT"]

    asyncio.run(_run())
