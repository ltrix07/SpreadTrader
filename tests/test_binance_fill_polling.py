from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from spread_arb.config import Settings
from spread_arb.exchanges.binance import BinanceExchange
from spread_arb.mean_reversion_engine import MeanRevPosition, MeanReversionEngine, RollingBaseline
from spread_arb.models import ExchangeName, OrderResult, PositionInfo, Quote, SpreadOrderResult


class DummyOpportunityStore:
    def __init__(self) -> None:
        self.records: list[object] = []

    def insert_paper_trade(self, record: object) -> None:
        self.records.append(record)


class StubClient:
    def __init__(
        self,
        *,
        exchange: ExchangeName,
        position: PositionInfo,
        close_error: Exception | None = None,
    ) -> None:
        self.exchange = exchange
        self.position = position
        self.close_error = close_error
        self.close_calls: list[tuple[str, str, Decimal, bool]] = []

    async def get_position(self, _symbol: str) -> PositionInfo:
        return self.position

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        self.close_calls.append((symbol, side, qty, close))
        if self.close_error is not None:
            raise self.close_error
        return OrderResult(
            exchange=self.exchange,
            symbol=symbol,
            side=side,
            filled_qty=qty,
            avg_price=self.position.entry_price or Decimal("100"),
            fee=Decimal("0.01"),
            fee_currency="USDT",
            order_id=f"close-{self.exchange.value}",
            timestamp=datetime.now(UTC),
            is_partial=False,
            raw_response={},
        )


class StubExecutionService:
    def __init__(
        self,
        *,
        clients: dict[ExchangeName, StubClient],
        entry_result: SpreadOrderResult | None = None,
        exit_error: Exception | None = None,
        stop_result: tuple[str, str] = ("long-stop", "short-stop"),
    ) -> None:
        self.clients = clients
        self.entry_result = entry_result
        self.exit_error = exit_error
        self.stop_result = stop_result

    async def calculate_notional(self, *, long_exchange: ExchangeName, short_exchange: ExchangeName) -> float:
        _ = long_exchange, short_exchange
        return 100.0

    async def execute_spread_entry(self, **_kwargs: object) -> SpreadOrderResult:
        assert self.entry_result is not None
        return self.entry_result

    async def place_protective_stops(self, **_kwargs: object) -> tuple[str, str]:
        return self.stop_result

    async def cancel_protective_stops(self, **_kwargs: object) -> None:
        return

    async def execute_spread_exit(self, **_kwargs: object) -> SpreadOrderResult:
        if self.exit_error is not None:
            raise self.exit_error
        raise AssertionError("execute_spread_exit should not be called in this scenario")

    async def get_trigger_fill_result(self, **_kwargs: object) -> OrderResult | None:
        return None


def _quote(*, exchange: ExchangeName, symbol: str, bid: str, ask: str) -> Quote:
    return Quote(
        exchange=exchange,
        symbol=symbol,
        best_bid_price=Decimal(bid),
        best_ask_price=Decimal(ask),
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("100"),
        receive_latency_ms=0.0,
        source_latency_ms=0.0,
        received_at=datetime.now(UTC),
    )


def _seed_ready_baseline(
    engine: MeanReversionEngine,
    *,
    symbol: str,
    long_exchange: ExchangeName,
    short_exchange: ExchangeName,
) -> None:
    baseline = RollingBaseline(window=deque(), window_size=engine.settings.mr_rolling_window)
    for sample in [0.15, 0.20, 0.25] * 10:
        baseline.update(sample)
    engine.baselines[(symbol, long_exchange, short_exchange)] = baseline


def _build_live_engine(
    *,
    execution_service: StubExecutionService,
    long_exchange: ExchangeName = ExchangeName.BINANCE,
    short_exchange: ExchangeName = ExchangeName.BITGET,
) -> MeanReversionEngine:
    quote_map = {
        (long_exchange, "BTCUSDT"): _quote(exchange=long_exchange, symbol="BTCUSDT", bid="99.9", ask="100.0"),
        (short_exchange, "BTCUSDT"): _quote(exchange=short_exchange, symbol="BTCUSDT", bid="101.0", ask="101.1"),
    }
    engine = MeanReversionEngine(
        settings=Settings(
            symbols=["BTCUSDT"],
            live_trading=True,
            mr_revalidation_delay_sec=0.01,
            mr_revalidation_min_spread_pct=0.0,
            mr_min_net_edge_pct=0.0,
            mr_rolling_window=30,
        ),
        opportunity_store=DummyOpportunityStore(),  # type: ignore[arg-type]
        get_latest_quote=lambda exchange, symbol: quote_map.get((exchange, symbol)),
        execution_service=execution_service,  # type: ignore[arg-type]
    )
    _seed_ready_baseline(
        engine,
        symbol="BTCUSDT",
        long_exchange=long_exchange,
        short_exchange=short_exchange,
    )
    return engine


async def _run_live_entry_signal(
    engine: MeanReversionEngine,
    *,
    long_exchange: ExchangeName,
    short_exchange: ExchangeName,
) -> None:
    long_quote = _quote(exchange=long_exchange, symbol="BTCUSDT", bid="99.9", ask="100.0")
    short_quote = _quote(exchange=short_exchange, symbol="BTCUSDT", bid="101.0", ask="101.1")
    spread_pct = float((short_quote.best_bid_price - long_quote.best_ask_price) / long_quote.best_ask_price) * 100.0
    now = datetime.now(UTC)
    engine._evaluate_signal(
        symbol="BTCUSDT",
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        long_quote=long_quote,
        short_quote=short_quote,
        spread_pct=spread_pct,
        direction="ab",
        now=now,
    )
    pending = engine.pending_entries_by_symbol["BTCUSDT"]
    await asyncio.wait_for(pending.task, timeout=1.0)


def test_wait_for_fill_returns_filled_status() -> None:
    async def _run() -> None:
        client = BinanceExchange(session=object())
        responses = iter([
            {"status": "NEW", "executedQty": "0", "avgPrice": "0"},
            {"status": "PARTIALLY_FILLED", "executedQty": "5", "avgPrice": "1.20"},
            {"status": "FILLED", "executedQty": "10", "avgPrice": "1.25"},
        ])

        async def fake_get_order(symbol: str, order_id: str) -> dict:
            _ = symbol, order_id
            return next(responses)

        client._get_order = fake_get_order  # type: ignore[method-assign]

        result = await client._wait_for_fill("BTCUSDT", "12345", timeout_sec=0.1, poll_interval_sec=0.0)
        assert result["status"] == "FILLED"
        assert result["executedQty"] == "10"

    asyncio.run(_run())


def test_wait_for_fill_timeout_returns_last_response() -> None:
    async def _run() -> None:
        client = BinanceExchange(session=object())

        async def fake_get_order(symbol: str, order_id: str) -> dict:
            _ = symbol, order_id
            return {"status": "NEW", "executedQty": "0", "avgPrice": "0"}

        client._get_order = fake_get_order  # type: ignore[method-assign]

        result = await client._wait_for_fill("BTCUSDT", "12345", timeout_sec=0.01, poll_interval_sec=0.001)
        assert result["status"] == "NEW"

    asyncio.run(_run())


def test_parse_order_response_derives_avg_price_from_cum_quote() -> None:
    client = BinanceExchange(session=object())
    result = client._parse_order_response(
        {
            "status": "FILLED",
            "executedQty": "10",
            "avgPrice": "0",
            "cumQuote": "12.50",
            "updateTime": int(datetime.now(UTC).timestamp() * 1000),
        },
        symbol="BTCUSDT",
        side="buy",
    )
    assert result.filled_qty == Decimal("10")
    assert result.avg_price == Decimal("1.25")


def test_engine_recovers_position_from_get_position_when_order_returns_zero() -> None:
    async def _run() -> None:
        now = datetime.now(UTC)
        entry_result = SpreadOrderResult(
            long_order=OrderResult(
                exchange=ExchangeName.BINANCE,
                symbol="BTCUSDT",
                side="buy",
                filled_qty=Decimal("0"),
                avg_price=Decimal("0"),
                fee=Decimal("0.01"),
                fee_currency="USDT",
                order_id="long-1",
                timestamp=now,
                is_partial=True,
                raw_response={},
            ),
            short_order=OrderResult(
                exchange=ExchangeName.BITGET,
                symbol="BTCUSDT",
                side="sell",
                filled_qty=Decimal("1"),
                avg_price=Decimal("101"),
                fee=Decimal("0.01"),
                fee_currency="USDT",
                order_id="short-1",
                timestamp=now,
                is_partial=False,
                raw_response={},
            ),
        )
        execution_service = StubExecutionService(
            clients={
                ExchangeName.BINANCE: StubClient(
                    exchange=ExchangeName.BINANCE,
                    position=PositionInfo(
                        exchange=ExchangeName.BINANCE,
                        symbol="BTCUSDT",
                        size=Decimal("5"),
                        entry_price=Decimal("100"),
                        unrealized_pnl=Decimal("0"),
                        leverage=3,
                    ),
                ),
                ExchangeName.BITGET: StubClient(
                    exchange=ExchangeName.BITGET,
                    position=PositionInfo(
                        exchange=ExchangeName.BITGET,
                        symbol="BTCUSDT",
                        size=Decimal("-1"),
                        entry_price=Decimal("101"),
                        unrealized_pnl=Decimal("0"),
                        leverage=3,
                    ),
                ),
            },
            entry_result=entry_result,
        )
        engine = _build_live_engine(execution_service=execution_service)

        await _run_live_entry_signal(
            engine,
            long_exchange=ExchangeName.BINANCE,
            short_exchange=ExchangeName.BITGET,
        )

        position = engine.open_positions_by_symbol["BTCUSDT"]
        assert position.actual_qty_long == Decimal("5")
        assert position.entry_long_price == 100.0
        assert position.actual_qty_short == Decimal("1")

    asyncio.run(_run())


def test_engine_aborts_when_zero_parse_and_no_real_position() -> None:
    async def _run() -> None:
        now = datetime.now(UTC)
        entry_result = SpreadOrderResult(
            long_order=OrderResult(
                exchange=ExchangeName.BINANCE,
                symbol="BTCUSDT",
                side="buy",
                filled_qty=Decimal("0"),
                avg_price=Decimal("0"),
                fee=Decimal("0.01"),
                fee_currency="USDT",
                order_id="long-1",
                timestamp=now,
                is_partial=True,
                raw_response={},
            ),
            short_order=OrderResult(
                exchange=ExchangeName.BITGET,
                symbol="BTCUSDT",
                side="sell",
                filled_qty=Decimal("1"),
                avg_price=Decimal("101"),
                fee=Decimal("0.01"),
                fee_currency="USDT",
                order_id="short-1",
                timestamp=now,
                is_partial=False,
                raw_response={},
            ),
        )
        bitget_client = StubClient(
            exchange=ExchangeName.BITGET,
            position=PositionInfo(
                exchange=ExchangeName.BITGET,
                symbol="BTCUSDT",
                size=Decimal("-1"),
                entry_price=Decimal("101"),
                unrealized_pnl=Decimal("0"),
                leverage=3,
            ),
        )
        execution_service = StubExecutionService(
            clients={
                ExchangeName.BINANCE: StubClient(
                    exchange=ExchangeName.BINANCE,
                    position=PositionInfo(
                        exchange=ExchangeName.BINANCE,
                        symbol="BTCUSDT",
                        size=Decimal("0"),
                        entry_price=Decimal("0"),
                        unrealized_pnl=Decimal("0"),
                        leverage=3,
                    ),
                ),
                ExchangeName.BITGET: bitget_client,
            },
            entry_result=entry_result,
        )
        engine = _build_live_engine(execution_service=execution_service)

        await _run_live_entry_signal(
            engine,
            long_exchange=ExchangeName.BINANCE,
            short_exchange=ExchangeName.BITGET,
        )

        assert engine.open_positions_by_symbol == {}
        assert bitget_client.close_calls == [("BTCUSDT", "buy", Decimal("1"), True)]

    asyncio.run(_run())


def test_close_retry_limit_prevents_infinite_loop(caplog: pytest.LogCaptureFixture) -> None:
    async def _run() -> None:
        long_client = StubClient(
            exchange=ExchangeName.BINANCE,
            position=PositionInfo(
                exchange=ExchangeName.BINANCE,
                symbol="BTCUSDT",
                size=Decimal("1"),
                entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                leverage=3,
            ),
            close_error=RuntimeError("close failed"),
        )
        short_client = StubClient(
            exchange=ExchangeName.BITGET,
            position=PositionInfo(
                exchange=ExchangeName.BITGET,
                symbol="BTCUSDT",
                size=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                leverage=3,
            ),
        )
        execution_service = StubExecutionService(
            clients={
                ExchangeName.BINANCE: long_client,
                ExchangeName.BITGET: short_client,
            },
            exit_error=RuntimeError("exit failed"),
        )
        engine = _build_live_engine(execution_service=execution_service)
        position = MeanRevPosition(
            symbol="BTCUSDT",
            long_exchange=ExchangeName.BINANCE,
            short_exchange=ExchangeName.BITGET,
            direction="ab",
            notional_usdt=100.0,
            opened_at=datetime.now(UTC),
            entry_long_price=100.0,
            entry_short_price=101.0,
            entry_spread_pct=1.0,
            entry_rolling_mean=0.2,
            entry_rolling_std=0.1,
            sigma_at_entry=2.0,
            take_profit_target=0.4,
            max_adverse_spread_pct=1.0,
            max_favorable_spread_pct=1.0,
            estimated_entry_fees_usdt=0.2,
            estimated_entry_slippage_usdt=0.1,
            actual_qty_long=Decimal("0"),
            actual_qty_short=Decimal("0"),
        )
        engine.open_positions_by_symbol[position.symbol] = position

        for _ in range(3):
            await engine._close_position(
                position=position,
                close_reason="stop_setup_failed",
                long_quote=_quote(exchange=ExchangeName.BINANCE, symbol="BTCUSDT", bid="99.8", ask="100.0"),
                short_quote=_quote(exchange=ExchangeName.BITGET, symbol="BTCUSDT", bid="101.0", ask="101.2"),
            )

        assert "BTCUSDT" not in engine.open_positions_by_symbol
        assert long_client.close_calls == [
            ("BTCUSDT", "sell", Decimal("1"), True),
            ("BTCUSDT", "sell", Decimal("1"), True),
            ("BTCUSDT", "sell", Decimal("1"), True),
        ]

    caplog.set_level("CRITICAL")
    asyncio.run(_run())
    assert "LIVE close retries exhausted" in caplog.text
