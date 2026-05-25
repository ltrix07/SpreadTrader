from __future__ import annotations

from datetime import UTC, datetime

from spread_arb.config import Settings
from spread_arb.mean_reversion_engine import MeanReversionEngine


class DummyStore:
    def insert_paper_trade(self, _record: object) -> None:
        return


def test_clock_injection_used_by_check_exits() -> None:
    fixed_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    settings = Settings(symbols=["BTCUSDT"])

    engine = MeanReversionEngine(
        settings=settings,
        opportunity_store=DummyStore(),  # type: ignore[arg-type]
        get_latest_quote=lambda _exchange, _symbol: None,
        clock=lambda: fixed_time,
    )
    assert engine._clock() == fixed_time


def test_clock_defaults_to_utc_now() -> None:
    settings = Settings(symbols=["BTCUSDT"])

    engine = MeanReversionEngine(
        settings=settings,
        opportunity_store=DummyStore(),  # type: ignore[arg-type]
        get_latest_quote=lambda _exchange, _symbol: None,
    )
    before = datetime.now(UTC)
    result = engine._clock()
    after = datetime.now(UTC)
    assert before <= result <= after
