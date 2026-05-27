from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from spread_arb.config import Settings
from spread_arb.execution import ExecutionError, ExecutionService
from spread_arb.models import ExchangeName, OrderResult, Quote


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


class RoundingClient:
    def __init__(self, exchange: ExchangeName, *, min_qty: str, step_size: str) -> None:
        self.exchange = exchange
        self.min_qty = Decimal(min_qty)
        self.step_size = Decimal(step_size)
        self.placed_qtys: list[Decimal] = []

    async def set_leverage(self, _symbol: str, _leverage: int) -> None:
        return

    async def get_min_order_qty(self, _symbol: str) -> Decimal:
        return self.min_qty

    async def get_qty_step_size(self, _symbol: str) -> Decimal:
        return self.step_size

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        assert close is False
        self.placed_qtys.append(qty)
        return OrderResult(
            exchange=self.exchange,
            symbol=symbol,
            side=side,
            filled_qty=qty,
            avg_price=Decimal("100"),
            fee=Decimal("0"),
            fee_currency="USDT",
            order_id=f"{self.exchange.value}-1",
            timestamp=datetime.now(UTC),
            is_partial=False,
            raw_response={},
        )


def test_execute_spread_entry_rounds_with_step_size_not_min_qty() -> None:
    async def _run() -> None:
        long_client = RoundingClient(ExchangeName.BYBIT, min_qty="5.0", step_size="0.1")
        short_client = RoundingClient(ExchangeName.OKX, min_qty="5.0", step_size="0.1")
        service = ExecutionService(
            settings=Settings(
                live_trading=True,
                symbols=["BTCUSDT"],
                max_notional_usdt=100.0,
            ),
            clients={
                ExchangeName.BYBIT: long_client,
                ExchangeName.OKX: short_client,
            },
        )

        result = await service.execute_spread_entry(
            symbol="BTCUSDT",
            long_exchange=ExchangeName.BYBIT,
            short_exchange=ExchangeName.OKX,
            notional_usdt=53.7,
            long_quote=_quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="9.9", ask="10.0"),
            short_quote=_quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="10.0", ask="10.1"),
        )

        assert long_client.placed_qtys == [Decimal("5.3")]
        assert short_client.placed_qtys == [Decimal("5.3")]
        assert result.long_order.filled_qty == Decimal("5.3")
        assert result.short_order.filled_qty == Decimal("5.3")

    asyncio.run(_run())


def test_execute_spread_entry_still_validates_min_qty_after_step_rounding() -> None:
    async def _run() -> None:
        service = ExecutionService(
            settings=Settings(
                live_trading=True,
                symbols=["BTCUSDT"],
                max_notional_usdt=100.0,
            ),
            clients={
                ExchangeName.BYBIT: RoundingClient(ExchangeName.BYBIT, min_qty="5.5", step_size="0.1"),
                ExchangeName.OKX: RoundingClient(ExchangeName.OKX, min_qty="5.0", step_size="0.1"),
            },
        )

        with pytest.raises(ExecutionError, match="below minimum 5.5"):
            await service.execute_spread_entry(
                symbol="BTCUSDT",
                long_exchange=ExchangeName.BYBIT,
                short_exchange=ExchangeName.OKX,
                notional_usdt=53.7,
                long_quote=_quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="9.9", ask="10.0"),
                short_quote=_quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="10.0", ask="10.1"),
            )

    asyncio.run(_run())
