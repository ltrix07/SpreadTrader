from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from spread_arb.config import Settings
from spread_arb.mean_reversion_engine import MeanReversionEngine, RollingBaseline, _estimate_net_funding_cost_pct
from spread_arb.models import ExchangeName, FundingInfo, Quote


class DummyOpportunityStore:
    def insert_paper_trade(self, _record: object) -> None:
        return


def _quote(
    *,
    exchange: ExchangeName,
    symbol: str,
    bid: str,
    ask: str,
    bid_size: str,
    ask_size: str,
    received_at: datetime,
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
        received_at=received_at,
    )


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


def _funding(
    *,
    exchange: ExchangeName,
    rate: str,
    next_funding_time: datetime,
    interval_hours: int = 8,
    symbol: str = "BTCUSDT",
) -> FundingInfo:
    return FundingInfo(
        exchange=exchange,
        symbol=symbol,
        funding_rate=Decimal(rate),
        next_funding_time=next_funding_time,
        funding_interval_hours=interval_hours,
        fetched_at=datetime.now(UTC),
    )


def test_estimate_net_funding_cost_no_payments_in_window() -> None:
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    hold_seconds = 300
    long_funding = _funding(
        exchange=ExchangeName.BYBIT,
        rate="0.0010",
        next_funding_time=now + timedelta(hours=8),
    )
    short_funding = _funding(
        exchange=ExchangeName.OKX,
        rate="0.0005",
        next_funding_time=now + timedelta(hours=8),
    )

    result = _estimate_net_funding_cost_pct(
        long_funding=long_funding,
        short_funding=short_funding,
        position_direction="ab",
        now=now,
        hold_seconds=hold_seconds,
    )
    assert result == 0.0


def test_estimate_net_funding_cost_one_payment_both_sides() -> None:
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    long_funding = _funding(
        exchange=ExchangeName.BYBIT,
        rate="0.0010",  # +0.10%
        next_funding_time=now + timedelta(seconds=60),
    )
    short_funding = _funding(
        exchange=ExchangeName.OKX,
        rate="0.0005",  # +0.05%
        next_funding_time=now + timedelta(seconds=120),
    )

    result = _estimate_net_funding_cost_pct(
        long_funding=long_funding,
        short_funding=short_funding,
        position_direction="ab",
        now=now,
        hold_seconds=300,
    )
    assert abs(result - 0.05) < 1e-12


def test_estimate_net_funding_cost_short_receives_more() -> None:
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    long_funding = _funding(
        exchange=ExchangeName.BYBIT,
        rate="0.0005",  # +0.05%
        next_funding_time=now + timedelta(seconds=30),
    )
    short_funding = _funding(
        exchange=ExchangeName.OKX,
        rate="0.0015",  # +0.15%
        next_funding_time=now + timedelta(seconds=30),
    )

    result = _estimate_net_funding_cost_pct(
        long_funding=long_funding,
        short_funding=short_funding,
        position_direction="ab",
        now=now,
        hold_seconds=120,
    )
    assert abs(result + 0.1) < 1e-12


@dataclass
class FakeFundingClient:
    funding: FundingInfo
    calls: int = 0

    async def get_funding_info(self, _symbol: str) -> FundingInfo:
        self.calls += 1
        return self.funding


class FakeExecutionService:
    def __init__(self, clients: dict[ExchangeName, object], notional: float = 100.0) -> None:
        self.clients = clients
        self.notional = notional
        self.entry_called = False

    async def calculate_notional(self, *, long_exchange: ExchangeName, short_exchange: ExchangeName) -> float:
        _ = long_exchange, short_exchange
        return self.notional

    async def execute_spread_entry(self, **_kwargs: object) -> object:
        self.entry_called = True
        raise RuntimeError("stop test before real entry")


def test_funding_filter_rejects_high_cost_position(caplog) -> None:
    async def _run() -> None:
        now = datetime.now(UTC)
        long_quote = _quote(
            exchange=ExchangeName.BYBIT,
            symbol="BTCUSDT",
            bid="100.00",
            ask="100.05",
            bid_size="50",
            ask_size="50",
            received_at=now,
        )
        short_quote = _quote(
            exchange=ExchangeName.OKX,
            symbol="BTCUSDT",
            bid="101.00",
            ask="101.05",
            bid_size="50",
            ask_size="50",
            received_at=now,
        )

        long_client = FakeFundingClient(
            funding=_funding(
                exchange=ExchangeName.BYBIT,
                rate="0.0050",  # +0.50%
                next_funding_time=now + timedelta(seconds=60),
            )
        )
        short_client = FakeFundingClient(
            funding=_funding(
                exchange=ExchangeName.OKX,
                rate="0.0000",  # 0.00%
                next_funding_time=now + timedelta(seconds=60),
            )
        )
        execution = FakeExecutionService(
            clients={
                ExchangeName.BYBIT: long_client,
                ExchangeName.OKX: short_client,
            },
        )

        settings = Settings(
            symbols=["BTCUSDT"],
            live_trading=True,
            mr_revalidation_delay_sec=0.05,
            mr_revalidation_min_spread_pct=0.0,
            mr_rolling_window=30,
            mr_min_net_edge_pct=0.0,
            mr_funding_filter_enabled=True,
            mr_funding_max_cost_fraction=0.30,
            mr_max_hold_seconds=300,
        )

        engine = MeanReversionEngine(
            settings=settings,
            opportunity_store=DummyOpportunityStore(),  # type: ignore[arg-type]
            get_latest_quote=lambda exchange, _symbol: long_quote if exchange == ExchangeName.BYBIT else short_quote,
            execution_service=execution,  # type: ignore[arg-type]
        )
        _seed_ready_baseline(
            engine,
            symbol="BTCUSDT",
            long_exchange=ExchangeName.BYBIT,
            short_exchange=ExchangeName.OKX,
            samples=[0.15, 0.20, 0.25] * 10,
        )

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

        assert engine.open_positions_by_symbol == {}
        assert execution.entry_called is False
        assert long_client.calls == 1
        assert short_client.calls == 1

    asyncio.run(_run())
    assert any("mr reval REJECT FUNDING" in record.message for record in caplog.records)


def test_funding_filter_disabled_does_not_check() -> None:
    async def _run() -> None:
        now = datetime.now(UTC)
        long_quote = _quote(
            exchange=ExchangeName.BYBIT,
            symbol="BTCUSDT",
            bid="100.00",
            ask="100.05",
            bid_size="50",
            ask_size="50",
            received_at=now,
        )
        short_quote = _quote(
            exchange=ExchangeName.OKX,
            symbol="BTCUSDT",
            bid="101.00",
            ask="101.05",
            bid_size="50",
            ask_size="50",
            received_at=now,
        )

        long_client = FakeFundingClient(
            funding=_funding(
                exchange=ExchangeName.BYBIT,
                rate="0.0050",
                next_funding_time=now + timedelta(seconds=60),
            )
        )
        short_client = FakeFundingClient(
            funding=_funding(
                exchange=ExchangeName.OKX,
                rate="0.0000",
                next_funding_time=now + timedelta(seconds=60),
            )
        )
        execution = FakeExecutionService(
            clients={
                ExchangeName.BYBIT: long_client,
                ExchangeName.OKX: short_client,
            },
        )

        settings = Settings(
            symbols=["BTCUSDT"],
            live_trading=True,
            mr_revalidation_delay_sec=0.05,
            mr_revalidation_min_spread_pct=0.0,
            mr_rolling_window=30,
            mr_min_net_edge_pct=0.0,
            mr_funding_filter_enabled=False,
        )

        engine = MeanReversionEngine(
            settings=settings,
            opportunity_store=DummyOpportunityStore(),  # type: ignore[arg-type]
            get_latest_quote=lambda exchange, _symbol: long_quote if exchange == ExchangeName.BYBIT else short_quote,
            execution_service=execution,  # type: ignore[arg-type]
        )
        _seed_ready_baseline(
            engine,
            symbol="BTCUSDT",
            long_exchange=ExchangeName.BYBIT,
            short_exchange=ExchangeName.OKX,
            samples=[0.15, 0.20, 0.25] * 10,
        )

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

        assert long_client.calls == 0
        assert short_client.calls == 0

    asyncio.run(_run())


def test_get_funding_info_uses_cache_ttl() -> None:
    async def _run() -> None:
        base_time = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        current_time = {"value": base_time}

        client = FakeFundingClient(
            funding=_funding(
                exchange=ExchangeName.BYBIT,
                rate="0.0010",
                next_funding_time=base_time + timedelta(hours=8),
            )
        )
        execution = SimpleNamespace(clients={ExchangeName.BYBIT: client})
        settings = Settings(symbols=["BTCUSDT"], live_trading=True)
        engine = MeanReversionEngine(
            settings=settings,
            opportunity_store=DummyOpportunityStore(),  # type: ignore[arg-type]
            get_latest_quote=lambda _exchange, _symbol: None,
            execution_service=execution,  # type: ignore[arg-type]
            clock=lambda: current_time["value"],
        )

        first = await engine._get_funding_info(ExchangeName.BYBIT, "BTCUSDT")
        second = await engine._get_funding_info(ExchangeName.BYBIT, "BTCUSDT")
        current_time["value"] = base_time + timedelta(seconds=61)
        third = await engine._get_funding_info(ExchangeName.BYBIT, "BTCUSDT")

        assert first is not None
        assert second is first
        assert third is not None
        assert client.calls == 2

    asyncio.run(_run())
