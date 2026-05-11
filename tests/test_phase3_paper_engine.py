from spread_arb.paper_engine import calculate_pnl, decide_close_reason


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

