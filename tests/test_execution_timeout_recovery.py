from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from spread_arb.config import Settings
from spread_arb.execution import ExecutionError, ExecutionService
from spread_arb.mean_reversion_engine import MeanReversionEngine, RollingBaseline
from spread_arb.models import ExchangeName, OrderResult, PositionInfo, Quote, SpreadOrderResult
from spread_arb.storage import PaperTradeRecord


class DummyOpportunityStore:
    def __init__(self) -> None:
        self.records: list[PaperTradeRecord] = []

    def insert_paper_trade(self, record: PaperTradeRecord) -> None:
        self.records.append(record)


def _quote(
    *,
    exchange: ExchangeName,
    symbol: str,
    bid: str,
    ask: str,
    bid_size: str = "50",
    ask_size: str = "50",
    received_at: datetime | None = None,
) -> Quote:
    return Quote(
        exchange=exchange,
        symbol=symbol,
        best_bid_price=Decimal(bid),
        best_ask_price=Decimal(ask),
        best_bid_size=Decimal(bid_size),
        best_ask_size=Decimal(ask_size),
        receive_latency_ms=0.0,
        source_latency_ms=0.0,
        received_at=received_at or datetime.now(UTC),
    )


class TimeoutRecoveryClient:
    def __init__(self, exchange: ExchangeName, position_size: Decimal, entry_price: Decimal) -> None:
        self.exchange = exchange
        self.position_size = position_size
        self.entry_price = entry_price
        self.close_calls: list[tuple[str, str, Decimal, bool]] = []

    async def set_leverage(self, _symbol: str, _leverage: int) -> None:
        return

    async def get_min_order_qty(self, _symbol: str) -> Decimal:
        return Decimal("0.001")

    async def get_qty_step_size(self, _symbol: str) -> Decimal:
        return Decimal("0.001")

    async def get_position(self, symbol: str) -> PositionInfo:
        return PositionInfo(
            exchange=self.exchange,
            symbol=symbol,
            size=self.position_size,
            entry_price=self.entry_price,
            unrealized_pnl=Decimal("0"),
            leverage=3,
        )

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        if close:
            self.close_calls.append((symbol, side, qty, close))
            return OrderResult(
                exchange=self.exchange,
                symbol=symbol,
                side=side,
                filled_qty=qty,
                avg_price=self.entry_price,
                fee=Decimal("0"),
                fee_currency="USDT",
                order_id=f"close-{self.exchange.value}",
                timestamp=datetime.now(UTC),
                is_partial=False,
                raw_response={},
            )
        await asyncio.sleep(0.2)
        raise AssertionError("open order coroutine should time out before returning")


class ExitTimeoutClient:
    def __init__(self, exchange: ExchangeName, position_size: Decimal) -> None:
        self.exchange = exchange
        self.position_size = position_size

    async def place_market_order(
        self,
        _symbol: str,
        _side: str,
        _qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        assert close is True
        await asyncio.sleep(0.2)
        raise AssertionError("close order coroutine should time out before returning")

    async def get_position(self, symbol: str) -> PositionInfo:
        return PositionInfo(
            exchange=self.exchange,
            symbol=symbol,
            size=self.position_size,
            entry_price=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            leverage=3,
        )


class FakeLiveExecutionService:
    def __init__(
        self,
        *,
        entry_result: SpreadOrderResult,
        exit_result: SpreadOrderResult | None = None,
        stop_result: tuple[str, str] = ("long-stop", "short-stop"),
        stop_error: Exception | None = None,
        exit_error: Exception | None = None,
        client_positions: dict[ExchangeName, PositionInfo] | None = None,
    ) -> None:
        self.entry_result = entry_result
        self.exit_result = exit_result or entry_result
        self.stop_result = stop_result
        self.stop_error = stop_error
        self.exit_error = exit_error
        positions = client_positions or {}
        self.clients = {
            ExchangeName.BYBIT: _StaticPositionClient(
                positions.get(
                    ExchangeName.BYBIT,
                    PositionInfo(
                        exchange=ExchangeName.BYBIT,
                        symbol="BTCUSDT",
                        size=Decimal("0"),
                        entry_price=Decimal("0"),
                        unrealized_pnl=Decimal("0"),
                        leverage=3,
                    ),
                )
            ),
            ExchangeName.OKX: _StaticPositionClient(
                positions.get(
                    ExchangeName.OKX,
                    PositionInfo(
                        exchange=ExchangeName.OKX,
                        symbol="BTCUSDT",
                        size=Decimal("0"),
                        entry_price=Decimal("0"),
                        unrealized_pnl=Decimal("0"),
                        leverage=3,
                    ),
                )
            ),
        }
        self.stop_calls = 0
        self.exit_calls = 0
        self.cancel_calls: list[tuple[str, ExchangeName, ExchangeName, str, str]] = []

    async def calculate_notional(self, *, long_exchange: ExchangeName, short_exchange: ExchangeName) -> float:
        _ = long_exchange, short_exchange
        return 100.0

    async def execute_spread_entry(self, **_kwargs: object) -> SpreadOrderResult:
        return self.entry_result

    async def place_protective_stops(self, **_kwargs: object) -> tuple[str, str]:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        return self.stop_result

    async def cancel_protective_stops(
        self,
        *,
        symbol: str,
        long_exchange: ExchangeName,
        short_exchange: ExchangeName,
        long_stop_id: str,
        short_stop_id: str,
    ) -> None:
        self.cancel_calls.append((symbol, long_exchange, short_exchange, long_stop_id, short_stop_id))

    async def execute_spread_exit(self, **_kwargs: object) -> SpreadOrderResult:
        self.exit_calls += 1
        if self.exit_error is not None:
            raise self.exit_error
        return self.exit_result


class _StaticPositionClient:
    def __init__(self, position: PositionInfo) -> None:
        self.position = position

    async def get_position(self, _symbol: str) -> PositionInfo:
        return self.position


def _seed_ready_baseline(
    engine: MeanReversionEngine,
    *,
    symbol: str,
    long_exchange: ExchangeName,
    short_exchange: ExchangeName,
    samples: list[float],
) -> None:
    baseline = RollingBaseline(window=deque(), window_size=engine.settings.mr_rolling_window)
    for sample in samples:
        baseline.update(sample)
    engine.baselines[(symbol, long_exchange, short_exchange)] = baseline


def _make_engine_for_live_entry(
    *,
    execution_service: FakeLiveExecutionService,
    opportunity_store: DummyOpportunityStore,
    long_quote: Quote,
    short_quote: Quote,
    now: datetime,
) -> MeanReversionEngine:
    quote_map = {
        (ExchangeName.BYBIT, "BTCUSDT"): long_quote,
        (ExchangeName.OKX, "BTCUSDT"): short_quote,
    }
    settings = Settings(
        symbols=["BTCUSDT"],
        live_trading=True,
        mr_revalidation_delay_sec=0.05,
        mr_revalidation_min_spread_pct=0.0,
        mr_rolling_window=30,
        mr_min_net_edge_pct=0.0,
    )
    engine = MeanReversionEngine(
        settings=settings,
        opportunity_store=opportunity_store,  # type: ignore[arg-type]
        get_latest_quote=lambda exchange, symbol: quote_map.get((exchange, symbol)),
        execution_service=execution_service,  # type: ignore[arg-type]
    )
    _seed_ready_baseline(
        engine,
        symbol="BTCUSDT",
        long_exchange=ExchangeName.BYBIT,
        short_exchange=ExchangeName.OKX,
        samples=[0.15, 0.20, 0.25] * 10,
    )
    return engine


async def _run_live_entry_signal(
    *,
    engine: MeanReversionEngine,
    long_quote: Quote,
    short_quote: Quote,
    now: datetime,
) -> None:
    spread_pct = float((short_quote.best_bid_price - long_quote.best_ask_price) / long_quote.best_ask_price) * 100.0
    engine._evaluate_signal(
        symbol="BTCUSDT",
        long_exchange=ExchangeName.BYBIT,
        short_exchange=ExchangeName.OKX,
        long_quote=long_quote,
        short_quote=short_quote,
        spread_pct=spread_pct,
        direction="ab",
        now=now,
    )
    pending = engine.pending_entries_by_symbol["BTCUSDT"]
    await asyncio.wait_for(pending.task, timeout=1.0)


def test_execute_spread_entry_timeout_recovers_when_both_legs_are_open() -> None:
    async def _run() -> None:
        settings = Settings(
            order_timeout_sec=0.01,
            live_trading=True,
            max_notional_usdt=200.0,
            symbols=["BTCUSDT"],
        )
        long_client = TimeoutRecoveryClient(ExchangeName.BYBIT, Decimal("1.25"), Decimal("100.5"))
        short_client = TimeoutRecoveryClient(ExchangeName.OKX, Decimal("-1.24"), Decimal("101.2"))
        service = ExecutionService(
            settings=settings,
            clients={
                ExchangeName.BYBIT: long_client,
                ExchangeName.OKX: short_client,
            },
        )

        result = await service.execute_spread_entry(
            symbol="BTCUSDT",
            long_exchange=ExchangeName.BYBIT,
            short_exchange=ExchangeName.OKX,
            notional_usdt=100.0,
            long_quote=_quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="100.4", ask="100.5"),
            short_quote=_quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="101.2", ask="101.3"),
        )

        assert result.long_order.raw_response["recovered_from_timeout"] is True
        assert result.short_order.raw_response["recovered_from_timeout"] is True
        assert result.long_order.filled_qty == Decimal("1.25")
        assert result.short_order.filled_qty == Decimal("1.24")
        assert long_client.close_calls == []
        assert short_client.close_calls == []

    asyncio.run(_run())


def test_execute_spread_entry_timeout_flattens_partial_open_exposure() -> None:
    async def _run() -> None:
        settings = Settings(
            order_timeout_sec=0.01,
            live_trading=True,
            max_notional_usdt=200.0,
            symbols=["BTCUSDT"],
        )
        long_client = TimeoutRecoveryClient(ExchangeName.BYBIT, Decimal("1.10"), Decimal("100.5"))
        short_client = TimeoutRecoveryClient(ExchangeName.OKX, Decimal("0"), Decimal("0"))
        service = ExecutionService(
            settings=settings,
            clients={
                ExchangeName.BYBIT: long_client,
                ExchangeName.OKX: short_client,
            },
        )

        with pytest.raises(ExecutionError, match="partial open exposure"):
            await service.execute_spread_entry(
                symbol="BTCUSDT",
                long_exchange=ExchangeName.BYBIT,
                short_exchange=ExchangeName.OKX,
                notional_usdt=100.0,
                long_quote=_quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="100.4", ask="100.5"),
                short_quote=_quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="101.2", ask="101.3"),
            )

        assert long_client.close_calls == [("BTCUSDT", "sell", Decimal("1.10"), True)]
        assert short_client.close_calls == []

    asyncio.run(_run())


def test_execute_spread_exit_timeout_marks_flat_positions_recovered() -> None:
    async def _run() -> None:
        settings = Settings(order_timeout_sec=0.01, live_trading=True, symbols=["BTCUSDT"])
        service = ExecutionService(
            settings=settings,
            clients={
                ExchangeName.BYBIT: ExitTimeoutClient(ExchangeName.BYBIT, Decimal("0")),
                ExchangeName.OKX: ExitTimeoutClient(ExchangeName.OKX, Decimal("0")),
            },
        )

        with pytest.raises(ExecutionError, match="both legs are flat") as exc_info:
            await service.execute_spread_exit(
                symbol="BTCUSDT",
                long_exchange=ExchangeName.BYBIT,
                short_exchange=ExchangeName.OKX,
                long_qty=Decimal("1"),
                short_qty=Decimal("1"),
            )

        assert exc_info.value.positions_flat is True

    asyncio.run(_run())


def test_live_entry_closes_position_when_stop_ids_are_missing() -> None:
    async def _run() -> None:
        now = datetime.now(UTC)
        long_quote = _quote(
            exchange=ExchangeName.BYBIT,
            symbol="BTCUSDT",
            bid="100.00",
            ask="100.05",
            received_at=now,
        )
        short_quote = _quote(
            exchange=ExchangeName.OKX,
            symbol="BTCUSDT",
            bid="101.00",
            ask="101.05",
            received_at=now,
        )
        spread_result = SpreadOrderResult(
            long_order=OrderResult(
                exchange=ExchangeName.BYBIT,
                symbol="BTCUSDT",
                side="buy",
                filled_qty=Decimal("1"),
                avg_price=Decimal("100.05"),
                fee=Decimal("0.10"),
                fee_currency="USDT",
                order_id="long-1",
                timestamp=now,
                is_partial=False,
                raw_response={},
            ),
            short_order=OrderResult(
                exchange=ExchangeName.OKX,
                symbol="BTCUSDT",
                side="sell",
                filled_qty=Decimal("1"),
                avg_price=Decimal("101.00"),
                fee=Decimal("0.10"),
                fee_currency="USDT",
                order_id="short-1",
                timestamp=now,
                is_partial=False,
                raw_response={},
            ),
        )
        exit_result = SpreadOrderResult(
            long_order=OrderResult(
                exchange=ExchangeName.BYBIT,
                symbol="BTCUSDT",
                side="sell",
                filled_qty=Decimal("1"),
                avg_price=Decimal("100.00"),
                fee=Decimal("0.10"),
                fee_currency="USDT",
                order_id="long-exit-1",
                timestamp=now,
                is_partial=False,
                raw_response={},
            ),
            short_order=OrderResult(
                exchange=ExchangeName.OKX,
                symbol="BTCUSDT",
                side="buy",
                filled_qty=Decimal("1"),
                avg_price=Decimal("101.05"),
                fee=Decimal("0.10"),
                fee_currency="USDT",
                order_id="short-exit-1",
                timestamp=now,
                is_partial=False,
                raw_response={},
            ),
        )
        execution_service = FakeLiveExecutionService(
            entry_result=spread_result,
            exit_result=exit_result,
            stop_result=("", "short-stop"),
        )
        opportunity_store = DummyOpportunityStore()
        engine = _make_engine_for_live_entry(
            execution_service=execution_service,
            opportunity_store=opportunity_store,
            long_quote=long_quote,
            short_quote=short_quote,
            now=now,
        )

        await _run_live_entry_signal(
            engine=engine,
            long_quote=long_quote,
            short_quote=short_quote,
            now=now,
        )

        assert "BTCUSDT" not in engine.open_positions_by_symbol
        assert execution_service.stop_calls == 1
        assert execution_service.exit_calls == 1
        assert execution_service.cancel_calls == [
            ("BTCUSDT", ExchangeName.BYBIT, ExchangeName.OKX, "", "short-stop")
        ]
        assert len(opportunity_store.records) == 1
        assert opportunity_store.records[0].close_reason == "stop_setup_failed"

    asyncio.run(_run())


def test_live_entry_closes_position_when_stop_placement_crashes() -> None:
    async def _run() -> None:
        now = datetime.now(UTC)
        long_quote = _quote(
            exchange=ExchangeName.BYBIT,
            symbol="BTCUSDT",
            bid="100.00",
            ask="100.05",
            received_at=now,
        )
        short_quote = _quote(
            exchange=ExchangeName.OKX,
            symbol="BTCUSDT",
            bid="101.00",
            ask="101.05",
            received_at=now,
        )
        spread_result = SpreadOrderResult(
            long_order=OrderResult(
                exchange=ExchangeName.BYBIT,
                symbol="BTCUSDT",
                side="buy",
                filled_qty=Decimal("1"),
                avg_price=Decimal("100.05"),
                fee=Decimal("0.10"),
                fee_currency="USDT",
                order_id="long-1",
                timestamp=now,
                is_partial=False,
                raw_response={},
            ),
            short_order=OrderResult(
                exchange=ExchangeName.OKX,
                symbol="BTCUSDT",
                side="sell",
                filled_qty=Decimal("1"),
                avg_price=Decimal("101.00"),
                fee=Decimal("0.10"),
                fee_currency="USDT",
                order_id="short-1",
                timestamp=now,
                is_partial=False,
                raw_response={},
            ),
        )
        execution_service = FakeLiveExecutionService(
            entry_result=spread_result,
            stop_error=RuntimeError("stop placement crashed"),
        )
        opportunity_store = DummyOpportunityStore()
        engine = _make_engine_for_live_entry(
            execution_service=execution_service,
            opportunity_store=opportunity_store,
            long_quote=long_quote,
            short_quote=short_quote,
            now=now,
        )

        await _run_live_entry_signal(
            engine=engine,
            long_quote=long_quote,
            short_quote=short_quote,
            now=now,
        )

        assert "BTCUSDT" not in engine.open_positions_by_symbol
        assert execution_service.stop_calls == 1
        assert execution_service.exit_calls == 1
        assert len(opportunity_store.records) == 1
        assert opportunity_store.records[0].close_reason == "stop_setup_failed"

    asyncio.run(_run())


def test_live_entry_keeps_tracked_position_when_emergency_close_fails(caplog) -> None:
    async def _run() -> None:
        now = datetime.now(UTC)
        long_quote = _quote(
            exchange=ExchangeName.BYBIT,
            symbol="BTCUSDT",
            bid="100.00",
            ask="100.05",
            received_at=now,
        )
        short_quote = _quote(
            exchange=ExchangeName.OKX,
            symbol="BTCUSDT",
            bid="101.00",
            ask="101.05",
            received_at=now,
        )
        spread_result = SpreadOrderResult(
            long_order=OrderResult(
                exchange=ExchangeName.BYBIT,
                symbol="BTCUSDT",
                side="buy",
                filled_qty=Decimal("1"),
                avg_price=Decimal("100.05"),
                fee=Decimal("0.10"),
                fee_currency="USDT",
                order_id="long-1",
                timestamp=now,
                is_partial=False,
                raw_response={},
            ),
            short_order=OrderResult(
                exchange=ExchangeName.OKX,
                symbol="BTCUSDT",
                side="sell",
                filled_qty=Decimal("1"),
                avg_price=Decimal("101.00"),
                fee=Decimal("0.10"),
                fee_currency="USDT",
                order_id="short-1",
                timestamp=now,
                is_partial=False,
                raw_response={},
            ),
        )
        execution_service = FakeLiveExecutionService(
            entry_result=spread_result,
            stop_error=RuntimeError("stop placement crashed"),
            exit_error=RuntimeError("exit crashed"),
        )
        opportunity_store = DummyOpportunityStore()
        engine = _make_engine_for_live_entry(
            execution_service=execution_service,
            opportunity_store=opportunity_store,
            long_quote=long_quote,
            short_quote=short_quote,
            now=now,
        )

        await _run_live_entry_signal(
            engine=engine,
            long_quote=long_quote,
            short_quote=short_quote,
            now=now,
        )

        position = engine.open_positions_by_symbol["BTCUSDT"]
        assert position.entry_long_price == 100.05
        assert position.entry_short_price == 101.0
        assert execution_service.stop_calls == 1
        assert execution_service.exit_calls == 1
        assert opportunity_store.records == []

    caplog.set_level(logging.CRITICAL)
    asyncio.run(_run())
    assert any("LIVE position still open after protective-stop failure" in record.message for record in caplog.records)
