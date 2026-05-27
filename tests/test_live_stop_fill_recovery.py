from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from spread_arb.config import Settings
from spread_arb.execution import ExecutionError
from spread_arb.mean_reversion_engine import MeanRevPosition, MeanReversionEngine
from spread_arb.models import ExchangeName, FundingInfo, OrderResult, PositionInfo, Quote
from spread_arb.storage import PaperTradeRecord


class DummyOpportunityStore:
    def __init__(self) -> None:
        self.records: list[PaperTradeRecord] = []

    def insert_paper_trade(self, record: PaperTradeRecord) -> None:
        self.records.append(record)


class TriggerHistoryClient:
    def __init__(
        self,
        exchange: ExchangeName,
        trigger_fill: OrderResult | None,
        funding: FundingInfo | None = None,
    ) -> None:
        self.exchange = exchange
        self.trigger_fill = trigger_fill
        self.funding = funding

    async def get_position(self, symbol: str) -> PositionInfo:
        return PositionInfo(
            exchange=self.exchange,
            symbol=symbol,
            size=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            leverage=3,
        )

    async def get_trigger_fill_result(self, symbol: str, trigger_order_id: str) -> OrderResult | None:
        _ = symbol, trigger_order_id
        return self.trigger_fill

    async def get_funding_info(self, symbol: str) -> FundingInfo:
        _ = symbol
        if self.funding is None:
            raise RuntimeError("no funding configured")
        return self.funding


class StopRecoveryExecutionService:
    def __init__(self, clients: dict[ExchangeName, TriggerHistoryClient]) -> None:
        self.clients = clients

    async def cancel_protective_stops(self, **_kwargs: object) -> None:
        return

    async def execute_spread_exit(self, **_kwargs: object):
        raise ExecutionError("exit timeout after 1s")

    async def get_trigger_fill_result(
        self,
        *,
        exchange: ExchangeName,
        symbol: str,
        trigger_order_id: str,
    ) -> OrderResult | None:
        return await self.clients[exchange].get_trigger_fill_result(symbol, trigger_order_id)


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


def _position(now: datetime) -> MeanRevPosition:
    return MeanRevPosition(
        symbol="BTCUSDT",
        long_exchange=ExchangeName.BYBIT,
        short_exchange=ExchangeName.OKX,
        direction="ab",
        notional_usdt=100.0,
        opened_at=now,
        entry_long_price=100.0,
        entry_short_price=101.0,
        entry_spread_pct=1.0,
        entry_rolling_mean=0.3,
        entry_rolling_std=0.1,
        sigma_at_entry=2.0,
        take_profit_target=0.4,
        max_adverse_spread_pct=1.0,
        max_favorable_spread_pct=1.0,
        estimated_entry_fees_usdt=0.2,
        estimated_entry_slippage_usdt=0.1,
        actual_qty_long=Decimal("1"),
        actual_qty_short=Decimal("1"),
        long_stop_order_id="long-stop-1",
        short_stop_order_id="short-stop-1",
        long_stop_trigger_price=95.0,
        short_stop_trigger_price=106.05,
    )


def _funding(*, exchange: ExchangeName, rate: str, next_funding_time: datetime) -> FundingInfo:
    return FundingInfo(
        exchange=exchange,
        symbol="BTCUSDT",
        funding_rate=Decimal(rate),
        next_funding_time=next_funding_time,
        funding_interval_hours=8,
        fetched_at=next_funding_time,
    )


def test_close_position_uses_actual_trigger_fill_prices_when_available() -> None:
    async def _run() -> None:
        now = datetime.now(UTC)
        long_fill = OrderResult(
            exchange=ExchangeName.BYBIT,
            symbol="BTCUSDT",
            side="sell",
            filled_qty=Decimal("1"),
            avg_price=Decimal("94.5"),
            fee=Decimal("0.11"),
            fee_currency="USDT",
            order_id="long-exit",
            timestamp=now,
            is_partial=False,
            raw_response={},
        )
        short_fill = OrderResult(
            exchange=ExchangeName.OKX,
            symbol="BTCUSDT",
            side="buy",
            filled_qty=Decimal("1"),
            avg_price=Decimal("106.8"),
            fee=Decimal("0.12"),
            fee_currency="USDT",
            order_id="short-exit",
            timestamp=now,
            is_partial=False,
            raw_response={},
        )
        execution_service = StopRecoveryExecutionService(
            {
                ExchangeName.BYBIT: TriggerHistoryClient(ExchangeName.BYBIT, long_fill),
                ExchangeName.OKX: TriggerHistoryClient(ExchangeName.OKX, short_fill),
            }
        )
        opportunity_store = DummyOpportunityStore()
        engine = MeanReversionEngine(
            settings=Settings(symbols=["BTCUSDT"], live_trading=True),
            opportunity_store=opportunity_store,  # type: ignore[arg-type]
            get_latest_quote=lambda _exchange, _symbol: None,
            execution_service=execution_service,  # type: ignore[arg-type]
        )
        position = _position(now)
        engine.open_positions_by_symbol[position.symbol] = position

        await engine._close_position(
            position=position,
            close_reason="stop_loss",
            long_quote=_quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="97.0", ask="97.1"),
            short_quote=_quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="104.0", ask="104.1"),
        )

        record = opportunity_store.records[0]
        assert record.exit_long_price == 94.5
        assert record.exit_short_price == 106.8
        assert record.fees_usdt == pytest.approx(0.43)
        assert record.slippage_usdt == pytest.approx(0.1)

    asyncio.run(_run())


def test_close_position_falls_back_to_stop_trigger_prices_when_history_is_unavailable() -> None:
    async def _run() -> None:
        now = datetime.now(UTC)
        execution_service = StopRecoveryExecutionService(
            {
                ExchangeName.BYBIT: TriggerHistoryClient(ExchangeName.BYBIT, None),
                ExchangeName.OKX: TriggerHistoryClient(ExchangeName.OKX, None),
            }
        )
        opportunity_store = DummyOpportunityStore()
        engine = MeanReversionEngine(
            settings=Settings(symbols=["BTCUSDT"], live_trading=True, slippage_buffer_pct=0.05),
            opportunity_store=opportunity_store,  # type: ignore[arg-type]
            get_latest_quote=lambda _exchange, _symbol: None,
            execution_service=execution_service,  # type: ignore[arg-type]
        )
        position = _position(now)
        engine.open_positions_by_symbol[position.symbol] = position

        await engine._close_position(
            position=position,
            close_reason="stop_loss",
            long_quote=_quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="97.0", ask="97.1"),
            short_quote=_quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="104.0", ask="104.1"),
        )

        record = opportunity_store.records[0]
        assert record.exit_long_price == 95.0
        assert record.exit_short_price == 106.05
        assert record.slippage_usdt > 0.0

    asyncio.run(_run())


def test_close_position_hydrates_missing_funding_before_recording_trade() -> None:
    async def _run() -> None:
        now = datetime.now(UTC)
        execution_service = StopRecoveryExecutionService(
            {
                ExchangeName.BYBIT: TriggerHistoryClient(
                    ExchangeName.BYBIT,
                    None,
                    funding=_funding(
                        exchange=ExchangeName.BYBIT,
                        rate="0.0020",
                        next_funding_time=now - timedelta(hours=2),
                    ),
                ),
                ExchangeName.OKX: TriggerHistoryClient(
                    ExchangeName.OKX,
                    None,
                    funding=_funding(
                        exchange=ExchangeName.OKX,
                        rate="0.0005",
                        next_funding_time=now - timedelta(hours=2),
                    ),
                ),
            }
        )
        opportunity_store = DummyOpportunityStore()
        engine = MeanReversionEngine(
            settings=Settings(symbols=["BTCUSDT"], live_trading=True, slippage_buffer_pct=0.05),
            opportunity_store=opportunity_store,  # type: ignore[arg-type]
            get_latest_quote=lambda _exchange, _symbol: None,
            execution_service=execution_service,  # type: ignore[arg-type]
        )
        position = _position(now - timedelta(hours=10))
        engine.open_positions_by_symbol[position.symbol] = position

        await engine._close_position(
            position=position,
            close_reason="stop_loss",
            long_quote=_quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="97.0", ask="97.1"),
            short_quote=_quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="104.0", ask="104.1"),
        )

        record = opportunity_store.records[0]
        assert record.funding_usdt > 0.0

    asyncio.run(_run())
