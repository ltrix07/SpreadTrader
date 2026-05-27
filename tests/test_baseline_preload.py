from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spread_arb.config import Settings
from spread_arb.mean_reversion_engine import MeanReversionEngine
from spread_arb.models import ExchangeName
from spread_arb.storage import OpportunityStore, SpreadSnapshotRecord, init_sqlite


class DummyOpportunityStore:
    def insert_paper_trade(self, _record: object) -> None:
        return


def _db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'preload.sqlite'}"


def _engine(*, symbols: list[str], exchanges: list[ExchangeName], window: int) -> MeanReversionEngine:
    return MeanReversionEngine(
        settings=Settings(
            symbols=symbols,
            exchanges=exchanges,
            mr_rolling_window=window,
        ),
        opportunity_store=DummyOpportunityStore(),  # type: ignore[arg-type]
        get_latest_quote=lambda _exchange, _symbol: None,
    )


def _insert_snapshot(
    store: OpportunityStore,
    *,
    timestamp: datetime,
    symbol: str,
    exchange_a: str,
    exchange_b: str,
    spread_ab: float,
    spread_ba: float,
) -> None:
    store.insert_spread_snapshots([
        SpreadSnapshotRecord(
            timestamp=timestamp.isoformat(),
            symbol=symbol,
            exchange_a=exchange_a,
            exchange_b=exchange_b,
            bid_a=100.0,
            ask_a=100.0,
            bid_b=100.0,
            ask_b=100.0,
            raw_spread_ab_pct=spread_ab,
            raw_spread_ba_pct=spread_ba,
            best_raw_spread_pct=max(spread_ab, spread_ba),
            quote_age_a_ms=100.0,
            quote_age_b_ms=100.0,
        ),
    ])


def test_preload_baselines_loads_full_window_per_pair(tmp_path: Path) -> None:
    database_url = _db_url(tmp_path)
    init_sqlite(database_url)
    base_ts = datetime(2026, 5, 27, tzinfo=UTC)

    with OpportunityStore(database_url) as store:
        for index in range(30):
            spread = 0.10 + (index * 0.01)
            _insert_snapshot(
                store,
                timestamp=base_ts + timedelta(seconds=index + 1),
                symbol="BTCUSDT",
                exchange_a="bybit",
                exchange_b="gate",
                spread_ab=spread,
                spread_ba=-spread,
            )

        for index in range(60):
            spread = 1.00 + (index * 0.01)
            _insert_snapshot(
                store,
                timestamp=base_ts + timedelta(seconds=index + 100),
                symbol="BTCUSDT",
                exchange_a="bybit",
                exchange_b="okx",
                spread_ab=spread,
                spread_ba=-spread,
            )

    engine = _engine(
        symbols=["BTCUSDT"],
        exchanges=[ExchangeName.BYBIT, ExchangeName.OKX, ExchangeName.GATE],
        window=30,
    )
    engine.preload_baselines(database_url)

    bybit_gate = engine.baselines[("BTCUSDT", ExchangeName.BYBIT, ExchangeName.GATE)]
    gate_bybit = engine.baselines[("BTCUSDT", ExchangeName.GATE, ExchangeName.BYBIT)]
    bybit_okx = engine.baselines[("BTCUSDT", ExchangeName.BYBIT, ExchangeName.OKX)]

    assert bybit_gate.is_ready is True
    assert gate_bybit.is_ready is True
    assert bybit_okx.is_ready is True
    assert list(bybit_gate.window) == pytest.approx([0.10 + (index * 0.01) for index in range(30)])
    assert list(gate_bybit.window) == pytest.approx([-(0.10 + (index * 0.01)) for index in range(30)])
    assert list(bybit_okx.window) == pytest.approx([1.30 + (index * 0.01) for index in range(30)])


def test_preload_baselines_for_symbols_scopes_to_requested_symbols(tmp_path: Path) -> None:
    database_url = _db_url(tmp_path)
    init_sqlite(database_url)
    base_ts = datetime(2026, 5, 27, tzinfo=UTC)

    with OpportunityStore(database_url) as store:
        for index in range(30):
            spread = 0.50 + (index * 0.01)
            _insert_snapshot(
                store,
                timestamp=base_ts + timedelta(seconds=index + 1),
                symbol="BTCUSDT",
                exchange_a="bybit",
                exchange_b="okx",
                spread_ab=spread,
                spread_ba=-spread,
            )
        for index in range(40):
            spread = 1.50 + (index * 0.01)
            _insert_snapshot(
                store,
                timestamp=base_ts + timedelta(seconds=index + 100),
                symbol="ETHUSDT",
                exchange_a="bybit",
                exchange_b="okx",
                spread_ab=spread,
                spread_ba=-spread,
            )

    engine = _engine(
        symbols=["BTCUSDT", "ETHUSDT"],
        exchanges=[ExchangeName.BYBIT, ExchangeName.OKX],
        window=30,
    )
    engine.preload_baselines_for_symbols(database_url, ["ETHUSDT"])

    assert ("ETHUSDT", ExchangeName.BYBIT, ExchangeName.OKX) in engine.baselines
    assert ("ETHUSDT", ExchangeName.OKX, ExchangeName.BYBIT) in engine.baselines
    assert ("BTCUSDT", ExchangeName.BYBIT, ExchangeName.OKX) not in engine.baselines
    assert list(engine.baselines[("ETHUSDT", ExchangeName.BYBIT, ExchangeName.OKX)].window) == pytest.approx(
        [1.60 + (index * 0.01) for index in range(30)]
    )
