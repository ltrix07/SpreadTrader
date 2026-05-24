"""
Model different filter combinations against lifecycle data.

Simulates what the bot would see with various (net_edge, delay, floor) settings.
Uses spread_ticks to check what spread looks like after N seconds.

Usage:
    python scripts/model_filters.py [--db data/spread_lifecycle.sqlite3]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/spread_lifecycle.sqlite3")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # Basic stats
    cur.execute("SELECT COUNT(*) FROM spread_events")
    total_events = cur.fetchone()[0]
    cur.execute("SELECT MIN(started_at), MAX(ended_at) FROM spread_events")
    row = cur.fetchone()
    t0 = datetime.fromisoformat(row[0])
    t1 = datetime.fromisoformat(row[1])
    hours = (t1 - t0).total_seconds() / 3600

    print(f"=== Lifecycle data: {total_events} events over {hours:.1f} hours ===")
    print(f"    {row[0]}  →  {row[1]}")
    print()

    # Fee assumptions (same as bot)
    FEE_LONG = 0.05
    FEE_SHORT = 0.05
    SLIPPAGE = 0.01
    SAFETY = 0.01
    ROUNDTRIP_COST = 2 * FEE_LONG + 2 * FEE_SHORT + SLIPPAGE + SAFETY  # 0.22%

    DELAY_SEC = 10
    DELAY_MS_LO = (DELAY_SEC - 1) * 1000  # 9000
    DELAY_MS_HI = (DELAY_SEC + 1) * 1000  # 11000

    print(f"Roundtrip cost: {ROUNDTRIP_COST:.2f}%")
    print(f"Re-validation delay: {DELAY_SEC}s (tick window: {DELAY_MS_LO}-{DELAY_MS_HI}ms)")
    print()

    # For each event, get:
    # - entry_spread_pct (spread when event started, i.e. first crossed threshold)
    # - peak_spread_pct
    # - spread at ~10s mark
    # - simulated_net_pnl_pct (from lifecycle collector)
    # - duration_ms
    # - mean_spread_pct

    # We approximate "rolling_mean" as the threshold_pct from lifecycle
    # (the collector uses a fixed threshold, the bot uses rolling mean + sigma)
    # So we model: signal fires if peak >= X, and at 10s mark spread is still >= floor

    # Step 1: load all events lasting > DELAY
    cur.execute("""
        SELECT id, symbol, long_exchange, short_exchange,
               entry_spread_pct, peak_spread_pct, exit_spread_pct,
               mean_spread_pct, duration_ms, simulated_net_pnl_pct,
               threshold_pct, num_samples
        FROM spread_events
        WHERE duration_ms > ? * 1000
    """, (DELAY_SEC,))
    raw_events = cur.fetchall()
    print(f"Events lasting > {DELAY_SEC}s: {len(raw_events)}")

    # Step 2: batch-load ticks near the delay mark
    print("Loading ticks at ~10s mark...")
    cur.execute("""
        SELECT event_id, spread_pct, elapsed_ms
        FROM spread_ticks
        WHERE elapsed_ms BETWEEN ? AND ?
    """, (DELAY_MS_LO, DELAY_MS_HI))
    tick_rows = cur.fetchall()

    # Pick closest tick to DELAY_SEC*1000 per event
    best_tick: dict[int, tuple[float, int]] = {}
    target_ms = DELAY_SEC * 1000
    for tr in tick_rows:
        eid, sp, ems = tr[0], tr[1], tr[2]
        if eid not in best_tick or abs(ems - target_ms) < abs(best_tick[eid][1] - target_ms):
            best_tick[eid] = (sp, ems)

    # Build final event list as dicts
    events = []
    for ev in raw_events:
        eid = ev[0]
        tick = best_tick.get(eid)
        events.append({
            "id": eid,
            "symbol": ev[1],
            "long_exchange": ev[2],
            "short_exchange": ev[3],
            "entry_spread_pct": ev[4],
            "peak_spread_pct": ev[5],
            "exit_spread_pct": ev[6],
            "mean_spread_pct": ev[7],
            "duration_ms": ev[8],
            "simulated_net_pnl_pct": ev[9],
            "threshold_pct": ev[10],
            "num_samples": ev[11],
            "spread_at_delay": tick[0] if tick else None,
        })

    print(f"Events with tick at ~{DELAY_SEC}s: {sum(1 for e in events if e['spread_at_delay'] is not None)}")
    print()

    # Model different filter scenarios
    scenarios = [
        # (label, min_entry_spread, net_edge, floor_after_delay)
        ("CURRENT:  net_edge=0.25, no delay",       0.30, 0.25, 0.00),
        ("OPTION A: net_edge=0.25, floor=0.40",     0.30, 0.25, 0.40),
        ("OPTION B: net_edge=0.20, floor=0.40",     0.30, 0.20, 0.40),
        ("OPTION C: net_edge=0.15, floor=0.40",     0.30, 0.15, 0.40),
        ("OPTION D: net_edge=0.10, floor=0.40",     0.30, 0.10, 0.40),
        ("OPTION E: net_edge=0.15, floor=0.35",     0.30, 0.15, 0.35),
    ]

    print(f"{'Scenario':<42} {'Signals':>8} {'Pass':>8} {'Rate':>6} {'/hour':>6} {'WinRate':>8} {'AvgPnL':>8} {'AvgSpread@10s':>14}")
    print("-" * 115)

    for label, min_entry, net_edge_min, floor in scenarios:
        signals = 0
        passed = 0
        wins = 0
        total_pnl = 0.0
        total_spread_at_delay = 0.0

        for ev in events:
            entry_spread = ev["entry_spread_pct"]
            peak = ev["peak_spread_pct"]
            spread_at_10s = ev["spread_at_delay"]
            net_pnl = ev["simulated_net_pnl_pct"]
            threshold = ev["threshold_pct"]  # lifecycle collector threshold (proxy for mean)

            # Would the scanner pass it?
            if entry_spread < min_entry:
                continue

            # Would the signal pass net_edge at signal time?
            # net_edge = spread - mean - roundtrip_cost
            # We approximate mean ≈ threshold (lifecycle uses fixed threshold as baseline)
            # Actually for the bot, mean is rolling_mean. We don't have that.
            # Use entry_spread directly: net_edge_at_signal = entry_spread - threshold - roundtrip
            net_edge_at_signal = entry_spread - threshold - ROUNDTRIP_COST
            if net_edge_at_signal < net_edge_min:
                continue

            signals += 1

            if floor <= 0:
                # No delay — simulate immediate entry
                passed += 1
                total_spread_at_delay += entry_spread
                if net_pnl > 0:
                    wins += 1
                total_pnl += net_pnl
                continue

            # With delay: check spread at ~10s
            if spread_at_10s is None:
                continue

            # After delay, check floor
            if spread_at_10s < floor:
                continue

            # After delay, re-check net_edge
            net_edge_at_delay = spread_at_10s - threshold - ROUNDTRIP_COST
            if net_edge_at_delay < net_edge_min:
                continue

            passed += 1
            total_spread_at_delay += spread_at_10s
            if net_pnl > 0:
                wins += 1
            total_pnl += net_pnl

        wr = (wins / passed * 100) if passed > 0 else 0
        avg_pnl = (total_pnl / passed) if passed > 0 else 0
        per_hour = passed / hours if hours > 0 else 0
        avg_spread = (total_spread_at_delay / passed) if passed > 0 else 0
        pass_rate = (passed / signals * 100) if signals > 0 else 0

        print(f"{label:<42} {signals:>8} {passed:>8} {pass_rate:>5.1f}% {per_hour:>5.1f} {wr:>7.1f}% {avg_pnl:>+7.3f}% {avg_spread:>13.4f}%")

    print()

    # Detailed breakdown for recommended scenario (C)
    print("=" * 80)
    print("DETAILED: Option C (net_edge=0.15, floor=0.40, delay=10s)")
    print("=" * 80)

    # Per-pair stats
    pair_stats: dict[str, dict] = {}
    for ev in events:
        entry_spread = ev["entry_spread_pct"]
        spread_at_10s = ev["spread_at_delay"]
        net_pnl = ev["simulated_net_pnl_pct"]
        threshold = ev["threshold_pct"]
        pair = f"{ev['long_exchange']}->{ev['short_exchange']}"

        if entry_spread < 0.30:
            continue
        net_edge_at_signal = entry_spread - threshold - ROUNDTRIP_COST
        if net_edge_at_signal < 0.15:
            continue
        if spread_at_10s is None or spread_at_10s < 0.40:
            continue
        net_edge_at_delay = spread_at_10s - threshold - ROUNDTRIP_COST
        if net_edge_at_delay < 0.15:
            continue

        if pair not in pair_stats:
            pair_stats[pair] = {"count": 0, "wins": 0, "pnl_sum": 0.0}
        pair_stats[pair]["count"] += 1
        if net_pnl > 0:
            pair_stats[pair]["wins"] += 1
        pair_stats[pair]["pnl_sum"] += net_pnl

    print(f"\n{'Pair':<25} {'Count':>6} {'WinRate':>8} {'AvgPnL':>8}")
    print("-" * 50)
    for pair in sorted(pair_stats, key=lambda p: pair_stats[p]["count"], reverse=True):
        s = pair_stats[pair]
        wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
        avg = s["pnl_sum"] / s["count"] if s["count"] > 0 else 0
        print(f"{pair:<25} {s['count']:>6} {wr:>7.1f}% {avg:>+7.3f}%")

    # Per-symbol top 10
    sym_stats: dict[str, dict] = {}
    for ev in events:
        entry_spread = ev["entry_spread_pct"]
        spread_at_10s = ev["spread_at_delay"]
        net_pnl = ev["simulated_net_pnl_pct"]
        threshold = ev["threshold_pct"]
        sym = ev["symbol"]

        if entry_spread < 0.30:
            continue
        net_edge_at_signal = entry_spread - threshold - ROUNDTRIP_COST
        if net_edge_at_signal < 0.15:
            continue
        if spread_at_10s is None or spread_at_10s < 0.40:
            continue
        net_edge_at_delay = spread_at_10s - threshold - ROUNDTRIP_COST
        if net_edge_at_delay < 0.15:
            continue

        if sym not in sym_stats:
            sym_stats[sym] = {"count": 0, "wins": 0, "pnl_sum": 0.0}
        sym_stats[sym]["count"] += 1
        if net_pnl > 0:
            sym_stats[sym]["wins"] += 1
        sym_stats[sym]["pnl_sum"] += net_pnl

    print(f"\n{'Symbol':<20} {'Count':>6} {'WinRate':>8} {'AvgPnL':>8}")
    print("-" * 45)
    for sym in sorted(sym_stats, key=lambda s: sym_stats[s]["count"], reverse=True)[:15]:
        s = sym_stats[sym]
        wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
        avg = s["pnl_sum"] / s["count"] if s["count"] > 0 else 0
        print(f"{sym:<20} {s['count']:>6} {wr:>7.1f}% {avg:>+7.3f}%")

    db.close()


if __name__ == "__main__":
    main()
