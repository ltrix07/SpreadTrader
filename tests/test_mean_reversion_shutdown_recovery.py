from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from spread_arb.config import Settings
from spread_arb.mean_reversion_engine import MeanRevPosition, MeanReversionEngine
from spread_arb.models import ExchangeName, OrderResult, PositionInfo


class DummyOpportunityStore:
    def insert_paper_trade(self, _record: object) -> None:
        return


class FakeClient:
    def __init__(self, exchange: ExchangeName, position_size: Decimal, fail_close: bool = False) -> None:
        self.exchange = exchange
        self.position_size = position_size
        self.fail_close = fail_close
        self.placed_orders: list[tuple[str, str, Decimal, bool]] = []

    async def get_position(self, symbol: str) -> PositionInfo:
        return PositionInfo(
            exchange=self.exchange,
            symbol=symbol,
            size=self.position_size,
            entry_price=Decimal("100"),
            unrealized_pnl=Decimal("1.5"),
            leverage=3,
        )

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        self.placed_orders.append((symbol, side, qty, close))
        if self.fail_close:
            raise RuntimeError("close failed")
        return OrderResult(
            exchange=self.exchange,
            symbol=symbol,
            side=side,
            filled_qty=qty,
            avg_price=Decimal("100"),
            fee=Decimal("0.1"),
            fee_currency="USDT",
            order_id="order-1",
            timestamp=datetime.now(UTC),
            is_partial=False,
            raw_response={},
        )


def _build_position(symbol: str = "BTCUSDT") -> MeanRevPosition:
    return MeanRevPosition(
        symbol=symbol,
        long_exchange=ExchangeName.BYBIT,
        short_exchange=ExchangeName.OKX,
        direction="long_short",
        notional_usdt=100.0,
        opened_at=datetime.now(UTC),
        entry_long_price=100.0,
        entry_short_price=101.0,
        entry_spread_pct=1.0,
        entry_rolling_mean=0.5,
        entry_rolling_std=0.1,
        sigma_at_entry=3.0,
        take_profit_target=0.2,
        max_adverse_spread_pct=0.0,
        max_favorable_spread_pct=0.0,
        estimated_entry_fees_usdt=0.1,
        estimated_entry_slippage_usdt=0.05,
    )


def test_shutdown_closes_open_live_positions() -> None:
    settings = Settings(live_trading=True, symbols=["BTCUSDT"])
    engine = MeanReversionEngine(
        settings=settings,
        opportunity_store=DummyOpportunityStore(),  # type: ignore[arg-type]
        get_latest_quote=lambda _exchange, _symbol: None,
        execution_service=SimpleNamespace(clients={}),  # type: ignore[arg-type]
    )
    position = _build_position("BTCUSDT")
    engine.open_positions_by_symbol[position.symbol] = position
    called: list[str] = []
    summary_called = {"value": False}

    async def fake_close_position(*, position: MeanRevPosition, close_reason: str, long_quote: object, short_quote: object) -> None:
        called.append(close_reason)
        engine.open_positions_by_symbol.pop(position.symbol, None)

    def fake_log_summary() -> None:
        summary_called["value"] = True

    engine._close_position = fake_close_position  # type: ignore[method-assign]
    engine.log_summary = fake_log_summary  # type: ignore[method-assign]

    asyncio.run(engine.shutdown())

    assert called == ["shutdown"]
    assert "BTCUSDT" not in engine.open_positions_by_symbol
    assert summary_called["value"] is True


def test_shutdown_without_open_positions_is_noop() -> None:
    settings = Settings(live_trading=True, symbols=["BTCUSDT"])
    engine = MeanReversionEngine(
        settings=settings,
        opportunity_store=DummyOpportunityStore(),  # type: ignore[arg-type]
        get_latest_quote=lambda _exchange, _symbol: None,
        execution_service=SimpleNamespace(clients={}),  # type: ignore[arg-type]
    )
    summary_called = {"value": False}

    async def fail_if_called(**_kwargs: object) -> None:
        raise AssertionError("_close_position should not be called when no positions are open")

    def fake_log_summary() -> None:
        summary_called["value"] = True

    engine._close_position = fail_if_called  # type: ignore[method-assign]
    engine.log_summary = fake_log_summary  # type: ignore[method-assign]

    asyncio.run(engine.shutdown())

    assert summary_called["value"] is True


def test_recover_orphan_positions_closes_and_logs_failures(caplog) -> None:
    settings = Settings(live_trading=True, symbols=["BTCUSDT"])
    okx_client = FakeClient(exchange=ExchangeName.OKX, position_size=Decimal("2"))
    bybit_client = FakeClient(exchange=ExchangeName.BYBIT, position_size=Decimal("-1"), fail_close=True)
    execution_service = SimpleNamespace(
        clients={
            ExchangeName.OKX: okx_client,
            ExchangeName.BYBIT: bybit_client,
        }
    )
    engine = MeanReversionEngine(
        settings=settings,
        opportunity_store=DummyOpportunityStore(),  # type: ignore[arg-type]
        get_latest_quote=lambda _exchange, _symbol: None,
        execution_service=execution_service,  # type: ignore[arg-type]
    )
    caplog.set_level(logging.INFO)

    asyncio.run(engine.recover_orphan_positions())

    assert okx_client.placed_orders == [("BTCUSDT", "sell", Decimal("2"), True)]
    assert bybit_client.placed_orders == [("BTCUSDT", "buy", Decimal("1"), True)]
    assert any("FAILED to close orphan" in record.message for record in caplog.records)
