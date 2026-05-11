from spread_arb.opportunity import classify_opportunity


def test_rejected_when_net_spread_too_low() -> None:
    decision = classify_opportunity(
        estimated_net_spread_pct=0.10,
        entry_net_spread_pct=0.25,
        is_fresh=True,
        is_liquid=True,
    )
    assert decision.status == "rejected"
    assert decision.reason == "net_spread_too_low"


def test_observed_when_all_filters_pass() -> None:
    decision = classify_opportunity(
        estimated_net_spread_pct=0.40,
        entry_net_spread_pct=0.25,
        is_fresh=True,
        is_liquid=True,
    )
    assert decision.status == "observed"
    assert decision.reason is None


def test_rejected_when_stale_quote() -> None:
    decision = classify_opportunity(
        estimated_net_spread_pct=0.40,
        entry_net_spread_pct=0.25,
        is_fresh=False,
        is_liquid=True,
    )
    assert decision.status == "rejected"
    assert decision.reason == "stale_quote"


def test_rejected_when_insufficient_liquidity() -> None:
    decision = classify_opportunity(
        estimated_net_spread_pct=0.40,
        entry_net_spread_pct=0.25,
        is_fresh=True,
        is_liquid=False,
    )
    assert decision.status == "rejected"
    assert decision.reason == "insufficient_liquidity"

