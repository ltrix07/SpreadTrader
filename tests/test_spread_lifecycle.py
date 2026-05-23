from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from scripts.spread_lifecycle import (
    LifecycleDatabase,
    SpreadLifecycleTracker,
    compute_directional_spread_pct,
)


def test_spread_formula_matches_main_bot_logic() -> None:
    ask_long = 100.0
    bid_short = 101.0
    spread_pct = compute_directional_spread_pct(ask_long, bid_short)
    assert spread_pct == 1.0


def test_event_lifecycle_start_peak_cooldown_close(tmp_path) -> None:
    db = LifecycleDatabase(tmp_path / "spread_lifecycle.sqlite3")
    tracker = SpreadLifecycleTracker(
        threshold_pct=0.10,
        cooldown_sec=0.5,
        tick_interval_ms=200,
        fee_pct_by_exchange={
            "binance": 0.05,
            "bybit": 0.055,
        },
        slippage_buffer_pct=0.01,
        safety_buffer_pct=0.01,
        db=db,
    )

    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    tracker.process_update(
        symbol="BTCUSDT",
        long_exchange="binance",
        short_exchange="bybit",
        spread_pct=0.12,
        ts=t0,
        bid_short=101.0,
        ask_long=100.0,
        bbo_long_bps=2.0,
        bbo_short_bps=2.5,
    )
    tracker.process_update(
        symbol="BTCUSDT",
        long_exchange="binance",
        short_exchange="bybit",
        spread_pct=0.20,
        ts=t0 + timedelta(milliseconds=100),
        bid_short=101.2,
        ask_long=100.0,
        bbo_long_bps=2.0,
        bbo_short_bps=2.6,
    )
    tracker.process_update(
        symbol="BTCUSDT",
        long_exchange="binance",
        short_exchange="bybit",
        spread_pct=0.15,
        ts=t0 + timedelta(milliseconds=250),
        bid_short=101.1,
        ask_long=100.0,
        bbo_long_bps=1.9,
        bbo_short_bps=2.4,
    )
    tracker.process_update(
        symbol="BTCUSDT",
        long_exchange="binance",
        short_exchange="bybit",
        spread_pct=0.08,
        ts=t0 + timedelta(milliseconds=400),
        bid_short=100.8,
        ask_long=100.0,
        bbo_long_bps=1.8,
        bbo_short_bps=2.3,
    )
    tracker.process_update(
        symbol="BTCUSDT",
        long_exchange="binance",
        short_exchange="bybit",
        spread_pct=0.07,
        ts=t0 + timedelta(milliseconds=1000),
        bid_short=100.7,
        ask_long=100.0,
        bbo_long_bps=1.8,
        bbo_short_bps=2.2,
    )

    assert tracker.total_events_recorded == 1
    assert len(tracker.active_events) == 0

    with sqlite3.connect(tmp_path / "spread_lifecycle.sqlite3") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT *
            FROM spread_events
            WHERE symbol='BTCUSDT' AND long_exchange='binance' AND short_exchange='bybit'
            """
        ).fetchone()
        assert row is not None
        assert abs(float(row["entry_spread_pct"]) - 0.12) < 1e-9
        assert abs(float(row["peak_spread_pct"]) - 0.20) < 1e-9
        assert abs(float(row["exit_spread_pct"]) - 0.08) < 1e-9
        assert int(row["duration_ms"]) == 400
        assert int(row["num_samples"]) == 4

        ticks = conn.execute(
            """
            SELECT COUNT(*) FROM spread_ticks
            WHERE event_id = ?
            """,
            (row["id"],),
        ).fetchone()[0]
        assert ticks >= 3

    db.close()


def test_db_schema_created(tmp_path) -> None:
    db = LifecycleDatabase(tmp_path / "schema.sqlite3")
    with sqlite3.connect(tmp_path / "schema.sqlite3") as conn:
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "spread_events" in table_names
    assert "spread_ticks" in table_names
    db.close()

