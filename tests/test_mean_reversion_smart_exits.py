from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from spread_arb.config import Settings
from spread_arb.mean_reversion_engine import (
    MeanRevPosition,
    MeanReversionEngine,
    RollingBaseline,
    _bbo_walk_pct,
    _roundtrip_cost_pct,
)
from spread_arb.models import ExchangeName, Quote


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


def _build_engine(**kwargs: object) -> MeanReversionEngine:
    settings_kwargs: dict[str, object] = {
        "symbols": ["BTCUSDT"],
        "mr_rolling_window": 3,
        "mr_min_net_edge_pct": 0.0,
    }
    settings_kwargs.update(kwargs)
    settings = Settings(**settings_kwargs)
    return MeanReversionEngine(
        settings=settings,
        opportunity_store=DummyOpportunityStore(),  # type: ignore[arg-type]
        get_latest_quote=lambda _exchange, _symbol: None,
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


def _build_position(opened_seconds_ago: int) -> MeanRevPosition:
    return MeanRevPosition(
        symbol="BTCUSDT",
        long_exchange=ExchangeName.BYBIT,
        short_exchange=ExchangeName.OKX,
        direction="ab",
        notional_usdt=100.0,
        opened_at=datetime.now(UTC) - timedelta(seconds=opened_seconds_ago),
        entry_long_price=100.0,
        entry_short_price=101.0,
        entry_spread_pct=1.0,
        entry_rolling_mean=0.5,
        entry_rolling_std=0.2,
        sigma_at_entry=2.5,
        take_profit_target=0.4,
        max_adverse_spread_pct=0.0,
        max_favorable_spread_pct=0.0,
        estimated_entry_fees_usdt=0.1,
        estimated_entry_slippage_usdt=0.05,
    )


def test_timeout_pnl_guard_holds_position_in_loss() -> None:
    engine = _build_engine(mr_max_hold_seconds=10, mr_timeout_max_hold_seconds=30, mr_timeout_min_pnl_usdt=0.0)
    position = _build_position(opened_seconds_ago=15)
    engine.open_positions_by_symbol[position.symbol] = position
    close_reasons: list[str] = []

    def fake_schedule_close(*, position: MeanRevPosition, close_reason: str, long_quote: Quote | None, short_quote: Quote | None) -> None:
        close_reasons.append(close_reason)

    engine._schedule_close = fake_schedule_close  # type: ignore[method-assign]

    long_quote = _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="99.8", ask="100.0", bid_size="10", ask_size="10")
    short_quote = _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="100.2", ask="100.7", bid_size="10", ask_size="10")
    engine.check_exits({
        (ExchangeName.BYBIT, "BTCUSDT"): long_quote,
        (ExchangeName.OKX, "BTCUSDT"): short_quote,
    })

    assert close_reasons == []


def test_timeout_pnl_guard_closes_on_breakeven() -> None:
    engine = _build_engine(mr_max_hold_seconds=10, mr_timeout_max_hold_seconds=30, mr_timeout_min_pnl_usdt=0.0)
    position = _build_position(opened_seconds_ago=15)
    engine.open_positions_by_symbol[position.symbol] = position
    close_reasons: list[str] = []

    def fake_schedule_close(*, position: MeanRevPosition, close_reason: str, long_quote: Quote | None, short_quote: Quote | None) -> None:
        close_reasons.append(close_reason)

    engine._schedule_close = fake_schedule_close  # type: ignore[method-assign]

    long_quote = _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="101.0", ask="101.2", bid_size="10", ask_size="10")
    short_quote = _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="99.4", ask="99.5", bid_size="10", ask_size="10")
    engine.check_exits({
        (ExchangeName.BYBIT, "BTCUSDT"): long_quote,
        (ExchangeName.OKX, "BTCUSDT"): short_quote,
    })

    assert close_reasons == ["timeout"]


def test_timeout_hard_close_after_max_hold() -> None:
    engine = _build_engine(mr_max_hold_seconds=10, mr_timeout_max_hold_seconds=30, mr_timeout_min_pnl_usdt=0.0)
    position = _build_position(opened_seconds_ago=35)
    engine.open_positions_by_symbol[position.symbol] = position
    close_reasons: list[str] = []

    def fake_schedule_close(*, position: MeanRevPosition, close_reason: str, long_quote: Quote | None, short_quote: Quote | None) -> None:
        close_reasons.append(close_reason)

    engine._schedule_close = fake_schedule_close  # type: ignore[method-assign]

    long_quote = _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="99.8", ask="100.0", bid_size="10", ask_size="10")
    short_quote = _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="100.2", ask="100.7", bid_size="10", ask_size="10")
    engine.check_exits({
        (ExchangeName.BYBIT, "BTCUSDT"): long_quote,
        (ExchangeName.OKX, "BTCUSDT"): short_quote,
    })

    assert close_reasons == ["timeout_loss"]


def test_liquidity_filter_rejects_thin_top_of_book() -> None:
    engine = _build_engine(mr_notional_usdt=100.0, mr_min_top_capacity_multiplier=3.0, mr_max_bbo_spread_bps=15.0)
    _seed_ready_baseline(
        engine,
        symbol="BTCUSDT",
        long_exchange=ExchangeName.BYBIT,
        short_exchange=ExchangeName.OKX,
        samples=[0.2, 0.3, 0.4],
    )
    now = datetime.now(UTC)
    long_quote = _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="100.00", ask="100.03", bid_size="2", ask_size="2", received_at=now)
    short_quote = _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="101.00", ask="101.03", bid_size="2", ask_size="2", received_at=now)

    engine._evaluate_signal(
        symbol="BTCUSDT",
        long_exchange=ExchangeName.BYBIT,
        short_exchange=ExchangeName.OKX,
        long_quote=long_quote,
        short_quote=short_quote,
        spread_pct=1.0,
        direction="ab",
        now=now,
    )

    assert "BTCUSDT" not in engine.pending_entries_by_symbol


def test_liquidity_filter_doubles_multiplier_on_wide_bbo() -> None:
    engine = _build_engine(mr_notional_usdt=100.0, mr_min_top_capacity_multiplier=3.0, mr_max_bbo_spread_bps=15.0)
    _seed_ready_baseline(
        engine,
        symbol="BTCUSDT",
        long_exchange=ExchangeName.BYBIT,
        short_exchange=ExchangeName.OKX,
        samples=[0.2, 0.3, 0.4],
    )
    now = datetime.now(UTC)
    long_quote = _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="100.00", ask="100.10", bid_size="4", ask_size="3.5", received_at=now)
    short_quote = _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="101.00", ask="101.10", bid_size="3.5", ask_size="4", received_at=now)

    engine._evaluate_signal(
        symbol="BTCUSDT",
        long_exchange=ExchangeName.BYBIT,
        short_exchange=ExchangeName.OKX,
        long_quote=long_quote,
        short_quote=short_quote,
        spread_pct=1.0,
        direction="ab",
        now=now,
    )

    assert "BTCUSDT" not in engine.pending_entries_by_symbol


def test_revalidation_rejects_thin_liquidity() -> None:
    """_execute_after_delay must apply liquidity filter too."""
    async def _run() -> None:
        engine = _build_engine(
            mr_notional_usdt=100.0,
            mr_min_top_capacity_multiplier=3.0,
            mr_max_bbo_spread_bps=15.0,
            mr_revalidation_delay_sec=0.05,
            mr_revalidation_min_spread_pct=0.0,
        )
        _seed_ready_baseline(
            engine,
            symbol="BTCUSDT",
            long_exchange=ExchangeName.BYBIT,
            short_exchange=ExchangeName.OKX,
            samples=[0.2, 0.3, 0.4],
        )
        now = datetime.now(UTC)
        signal_long = _quote(
            exchange=ExchangeName.BYBIT,
            symbol="BTCUSDT",
            bid="100.00",
            ask="100.03",
            bid_size="50",
            ask_size="50",
            received_at=now,
        )
        signal_short = _quote(
            exchange=ExchangeName.OKX,
            symbol="BTCUSDT",
            bid="101.00",
            ask="101.03",
            bid_size="50",
            ask_size="50",
            received_at=now,
        )
        reval_long = _quote(
            exchange=ExchangeName.BYBIT,
            symbol="BTCUSDT",
            bid="100.00",
            ask="100.03",
            bid_size="2",
            ask_size="2",
            received_at=now,
        )
        reval_short = _quote(
            exchange=ExchangeName.OKX,
            symbol="BTCUSDT",
            bid="101.00",
            ask="101.03",
            bid_size="2",
            ask_size="2",
            received_at=now,
        )

        quotes_by_call: list[tuple[Quote, Quote]] = [(reval_long, reval_short)]

        def get_latest(exchange: ExchangeName, _symbol: str) -> Quote:
            long_q, short_q = quotes_by_call[0]
            return long_q if exchange == ExchangeName.BYBIT else short_q

        engine.get_latest_quote = get_latest

        engine._evaluate_signal(
            symbol="BTCUSDT",
            long_exchange=ExchangeName.BYBIT,
            short_exchange=ExchangeName.OKX,
            long_quote=signal_long,
            short_quote=signal_short,
            spread_pct=1.0,
            direction="ab",
            now=now,
        )
        assert "BTCUSDT" in engine.pending_entries_by_symbol

        pending = engine.pending_entries_by_symbol["BTCUSDT"]
        await asyncio.wait_for(pending.task, timeout=1.0)

        assert "BTCUSDT" not in engine.open_positions_by_symbol

    asyncio.run(_run())


def test_winning_counter_includes_timeout_wins() -> None:
    """Any profitable close should increment winning_trades_count."""
    async def _run() -> None:
        engine = _build_engine()
        position = _build_position(opened_seconds_ago=15)
        engine.open_positions_by_symbol[position.symbol] = position

        long_quote = _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="101.0", ask="101.2", bid_size="10", ask_size="10")
        short_quote = _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="99.4", ask="99.5", bid_size="10", ask_size="10")

        await engine._close_position(
            position=position,
            close_reason="timeout",
            long_quote=long_quote,
            short_quote=short_quote,
        )

        assert engine.winning_trades_count == 1
        assert engine.closed_trades_count == 1

    asyncio.run(_run())


def test_bbo_walk_reduces_net_edge() -> None:
    async def _run() -> None:
        engine = _build_engine(
            mr_notional_usdt=100.0,
            mr_min_top_capacity_multiplier=3.0,
            mr_max_bbo_spread_bps=15.0,
            mr_min_net_edge_pct=0.2,
            mr_revalidation_delay_sec=60.0,
        )
        _seed_ready_baseline(
            engine,
            symbol="BTCUSDT",
            long_exchange=ExchangeName.BYBIT,
            short_exchange=ExchangeName.OKX,
            samples=[0.15, 0.2, 0.25],
        )
        now = datetime.now(UTC)
        long_quote = _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="100.00", ask="100.05", bid_size="12", ask_size="12", received_at=now)
        short_quote = _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="100.90", ask="100.95", bid_size="12", ask_size="12", received_at=now)
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
        mean = engine.baselines[("BTCUSDT", ExchangeName.BYBIT, ExchangeName.OKX)].mean
        expected_edge_pct = spread_pct - mean
        roundtrip_cost_pct = _roundtrip_cost_pct(
            fee_long_pct=engine._get_fee_pct(ExchangeName.BYBIT),
            fee_short_pct=engine._get_fee_pct(ExchangeName.OKX),
            slippage_buffer_pct=engine.settings.slippage_buffer_pct,
            safety_buffer_pct=engine.settings.safety_buffer_pct,
        )
        bbo_walk_pct = _bbo_walk_pct(long_quote=long_quote, short_quote=short_quote)
        expected_net_edge_pct = expected_edge_pct - roundtrip_cost_pct - bbo_walk_pct

        assert pending.net_edge_pct == expected_net_edge_pct
        assert bbo_walk_pct > 0

        pending.task.cancel()
        await asyncio.gather(pending.task, return_exceptions=True)

    asyncio.run(_run())
