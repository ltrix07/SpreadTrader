from datetime import UTC, datetime, timedelta
from decimal import Decimal

from spread_arb.config import Settings
from spread_arb.models import ExchangeName, FundingInfo
from spread_arb.paper_engine import (
    PaperEngine,
    _one_side_fees_usdt,
    calculate_pnl,
    decide_close_reason,
    estimate_accrued_funding_usdt,
)


class DummyOpportunityStore:
    def insert_paper_trade(self, _record: object) -> None:
        return


def test_calculate_pnl_roundtrip_values() -> None:
    pnl = calculate_pnl(
        notional_usdt=100.0,
        entry_long_price=100.0,
        entry_short_price=100.0,
        exit_long_price=101.0,
        exit_short_price=99.0,
        entry_fees_usdt=0.10,
        exit_fees_usdt=0.10,
        entry_slippage_usdt=0.05,
        exit_slippage_usdt=0.05,
    )
    assert round(pnl.gross_pnl_usdt, 6) == 2.0
    assert round(pnl.fees_usdt, 6) == 0.2
    assert round(pnl.slippage_usdt, 6) == 0.1
    assert round(pnl.net_pnl_usdt, 6) == 1.7
    assert round(pnl.net_pnl_pct, 6) == 0.85


def test_calculate_pnl_subtracts_funding_from_net_result() -> None:
    pnl = calculate_pnl(
        notional_usdt=100.0,
        entry_long_price=100.0,
        entry_short_price=100.0,
        exit_long_price=101.0,
        exit_short_price=99.0,
        entry_fees_usdt=0.10,
        exit_fees_usdt=0.10,
        entry_slippage_usdt=0.05,
        exit_slippage_usdt=0.05,
        funding_usdt=0.25,
    )

    assert round(pnl.gross_pnl_usdt, 6) == 2.0
    assert round(pnl.funding_usdt, 6) == 0.25
    assert round(pnl.net_pnl_usdt, 6) == 1.45
    assert round(pnl.net_pnl_pct, 6) == 0.725


def test_estimate_accrued_funding_counts_payment_window() -> None:
    opened_at = datetime(2026, 1, 1, tzinfo=UTC)
    closed_at = opened_at + timedelta(hours=10)
    long_funding = FundingInfo(
        exchange=ExchangeName.BINANCE,
        symbol="BTCUSDT",
        funding_rate=Decimal("0.0015"),
        next_funding_time=opened_at + timedelta(hours=4),
        funding_interval_hours=8,
        fetched_at=opened_at,
    )
    short_funding = FundingInfo(
        exchange=ExchangeName.OKX,
        symbol="BTCUSDT",
        funding_rate=Decimal("0.0005"),
        next_funding_time=opened_at + timedelta(hours=4),
        funding_interval_hours=8,
        fetched_at=opened_at,
    )

    funding_usdt = estimate_accrued_funding_usdt(
        notional_usdt=100.0,
        long_funding=long_funding,
        short_funding=short_funding,
        window_start=opened_at,
        window_end=closed_at,
    )

    assert round(funding_usdt, 6) == 0.1


def test_close_rule_spread_converged() -> None:
    decision = decide_close_reason(
        current_raw_spread_pct=0.03,
        hold_seconds=5,
        has_fresh_quotes=True,
        exit_spread_pct=0.05,
        stop_spread_pct=0.80,
        max_hold_seconds=900,
    )
    assert decision.should_close is True
    assert decision.reason == "spread_converged"


def test_close_rule_stop_spread() -> None:
    decision = decide_close_reason(
        current_raw_spread_pct=0.90,
        hold_seconds=5,
        has_fresh_quotes=True,
        exit_spread_pct=0.05,
        stop_spread_pct=0.80,
        max_hold_seconds=900,
    )
    assert decision.should_close is True
    assert decision.reason == "stop_spread"


def test_close_rule_max_hold_time() -> None:
    decision = decide_close_reason(
        current_raw_spread_pct=0.30,
        hold_seconds=901,
        has_fresh_quotes=True,
        exit_spread_pct=0.05,
        stop_spread_pct=0.80,
        max_hold_seconds=900,
    )
    assert decision.should_close is True
    assert decision.reason == "max_hold_time"


def test_close_rule_stale_or_missing_quote() -> None:
    decision = decide_close_reason(
        current_raw_spread_pct=0.30,
        hold_seconds=10,
        has_fresh_quotes=False,
        exit_spread_pct=0.05,
        stop_spread_pct=0.80,
        max_hold_seconds=900,
    )
    assert decision.should_close is True
    assert decision.reason == "stale_or_missing_quote"


def test_paper_engine_fee_table_covers_all_supported_exchanges() -> None:
    engine = PaperEngine(
        settings=Settings(),
        opportunity_store=DummyOpportunityStore(),  # type: ignore[arg-type]
        get_latest_quote=lambda _exchange, _symbol: None,
    )

    assert engine.exchange_fees_pct[ExchangeName.BINANCE] == engine.settings.taker_fee_binance_pct
    assert engine.exchange_fees_pct[ExchangeName.OKX] == engine.settings.taker_fee_okx_pct
    assert engine.exchange_fees_pct[ExchangeName.GATE] == engine.settings.taker_fee_gate_pct
    assert engine.exchange_fees_pct[ExchangeName.BITGET] == engine.settings.taker_fee_bitget_pct
    assert engine.exchange_fees_pct[ExchangeName.HTX] == engine.settings.taker_fee_htx_pct


def test_one_side_fees_uses_nonzero_fees_for_non_bybit_non_mexc_exchanges() -> None:
    engine = PaperEngine(
        settings=Settings(taker_fee_binance_pct=0.05, taker_fee_okx_pct=0.07),
        opportunity_store=DummyOpportunityStore(),  # type: ignore[arg-type]
        get_latest_quote=lambda _exchange, _symbol: None,
    )

    fees = _one_side_fees_usdt(
        notional_usdt=100.0,
        fee_long_pct=engine.exchange_fees_pct[ExchangeName.BINANCE],
        fee_short_pct=engine.exchange_fees_pct[ExchangeName.OKX],
    )

    assert round(fees, 6) == 0.12
