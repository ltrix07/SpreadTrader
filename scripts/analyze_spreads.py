#!/usr/bin/env python3
"""Analyze spread snapshots collected by the scanner.

Usage:
    python scripts/analyze_spreads.py [--db data/spread_arb.sqlite3] [--top 20] [--sigma 2.0]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import math


@dataclass
class PairStats:
    symbol: str
    exchange_a: str
    exchange_b: str
    count: int
    mean_best_spread: float
    std_best_spread: float
    min_spread: float
    max_spread: float
    median_spread: float
    p95_spread: float
    p99_spread: float
    # How often spread exceeds threshold
    pct_above_2sigma: float
    pct_above_2_5sigma: float
    count_above_2sigma: int
    count_above_2_5sigma: int
    # Mean reversion potential: avg time spread stays above 2σ (in snapshots × 10s)
    avg_excursion_duration_snapshots: float
    # Net profitability estimate after fees
    mean_net_spread_at_2sigma: float  # mean spread when > 2σ minus roundtrip cost


def load_snapshots(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT timestamp, symbol, exchange_a, exchange_b,
               raw_spread_ab_pct, raw_spread_ba_pct, best_raw_spread_pct,
               quote_age_a_ms, quote_age_b_ms
        FROM spread_snapshots
        ORDER BY timestamp
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def compute_stats(
    snapshots: list[dict],
    sigma_threshold: float = 2.0,
    roundtrip_cost_pct: float = 0.30,
) -> list[PairStats]:
    """Compute statistics per (symbol, exchange_a, exchange_b) triple."""
    # Group by triple.
    groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for s in snapshots:
        key = (s["symbol"], s["exchange_a"], s["exchange_b"])
        groups[key].append(s["best_raw_spread_pct"])

    results: list[PairStats] = []
    for (symbol, ex_a, ex_b), spreads in groups.items():
        n = len(spreads)
        if n < 10:
            continue

        sorted_spreads = sorted(spreads)
        mean_val = sum(spreads) / n
        variance = sum((x - mean_val) ** 2 for x in spreads) / n
        std_val = math.sqrt(variance) if variance > 0 else 0.0001

        median_val = sorted_spreads[n // 2]
        p95_val = sorted_spreads[int(n * 0.95)]
        p99_val = sorted_spreads[int(n * 0.99)]

        threshold_2s = mean_val + sigma_threshold * std_val
        threshold_2_5s = mean_val + 2.5 * std_val

        above_2s = [x for x in spreads if x > threshold_2s]
        above_2_5s = [x for x in spreads if x > threshold_2_5s]

        # Estimate excursion duration: count consecutive runs above 2σ.
        excursion_lengths: list[int] = []
        current_run = 0
        for x in spreads:  # time-ordered
            if x > threshold_2s:
                current_run += 1
            else:
                if current_run > 0:
                    excursion_lengths.append(current_run)
                current_run = 0
        if current_run > 0:
            excursion_lengths.append(current_run)

        avg_excursion = (
            sum(excursion_lengths) / len(excursion_lengths)
            if excursion_lengths
            else 0.0
        )

        # Net spread when above 2σ (after fees).
        mean_spread_at_2s = (
            sum(above_2s) / len(above_2s) if above_2s else 0.0
        )
        mean_net_at_2s = mean_spread_at_2s - roundtrip_cost_pct

        results.append(PairStats(
            symbol=symbol,
            exchange_a=ex_a,
            exchange_b=ex_b,
            count=n,
            mean_best_spread=mean_val,
            std_best_spread=std_val,
            min_spread=sorted_spreads[0],
            max_spread=sorted_spreads[-1],
            median_spread=median_val,
            p95_spread=p95_val,
            p99_spread=p99_val,
            pct_above_2sigma=len(above_2s) / n * 100,
            pct_above_2_5sigma=len(above_2_5s) / n * 100,
            count_above_2sigma=len(above_2s),
            count_above_2_5sigma=len(above_2_5s),
            avg_excursion_duration_snapshots=avg_excursion,
            mean_net_spread_at_2sigma=mean_net_at_2s,
        ))

    return results


def print_report(
    stats: list[PairStats],
    top_n: int = 20,
    sigma: float = 2.0,
) -> None:
    total_snapshots = sum(s.count for s in stats)
    unique_symbols = len({s.symbol for s in stats})
    unique_pairs = len(stats)

    print("=" * 100)
    print("SPREAD SNAPSHOT ANALYSIS — MEAN REVERSION VIABILITY")
    print("=" * 100)
    print(f"Total snapshots: {total_snapshots:,}")
    print(f"Unique symbols:  {unique_symbols}")
    print(f"Unique pairs:    {unique_pairs}")
    if stats:
        print(f"Time span:       ~{stats[0].count * 10 / 3600:.1f} hours (at 10s intervals)")
    print()

    # ── Top pairs by frequency of 2σ+ excursions ──
    print(f"TOP {top_n} PAIRS BY FREQUENCY OF {sigma}σ+ SPREAD EXCURSIONS")
    print("-" * 100)
    ranked = sorted(stats, key=lambda s: s.count_above_2sigma, reverse=True)[:top_n]

    print(
        f"{'Symbol':<14s} {'Pair':<16s} "
        f"{'Mean%':>7s} {'Std%':>7s} {'P95%':>7s} {'Max%':>7s} "
        f"{'>{sigma}σ#':>6s} {'>{sigma}σ%':>6s} "
        f"{'AvgDur':>7s} {'NetAt{sigma}σ%':>9s} {'N':>6s}"
    )
    for s in ranked:
        pair = f"{s.exchange_a}->{s.exchange_b}"
        dur_sec = s.avg_excursion_duration_snapshots * 10
        dur_str = f"{dur_sec:.0f}s" if dur_sec < 120 else f"{dur_sec/60:.1f}m"
        print(
            f"{s.symbol:<14s} {pair:<16s} "
            f"{s.mean_best_spread:>7.4f} {s.std_best_spread:>7.4f} "
            f"{s.p95_spread:>7.4f} {s.max_spread:>7.4f} "
            f"{s.count_above_2sigma:>6d} {s.pct_above_2sigma:>5.1f}% "
            f"{dur_str:>7s} {s.mean_net_spread_at_2sigma:>+8.4f}% "
            f"{s.count:>6d}"
        )

    print()

    # ── Top pairs by NET profitability at 2σ ──
    profitable = [s for s in stats if s.mean_net_spread_at_2sigma > 0 and s.count_above_2sigma >= 3]
    profitable.sort(key=lambda s: s.mean_net_spread_at_2sigma * s.count_above_2sigma, reverse=True)

    print(f"TOP {top_n} POTENTIALLY PROFITABLE PAIRS (net > 0 at {sigma}σ, min 3 signals)")
    print("-" * 100)
    if not profitable:
        print("  No pairs found with positive net spread at threshold. "
              "Try lowering fees or collecting more data.")
    else:
        print(
            f"{'Symbol':<14s} {'Pair':<16s} "
            f"{'NetSpread%':>10s} {'Signals':>8s} {'AvgDur':>7s} "
            f"{'Est$/day':>9s} {'Mean%':>7s} {'Max%':>7s}"
        )
        for s in profitable[:top_n]:
            pair = f"{s.exchange_a}->{s.exchange_b}"
            dur_sec = s.avg_excursion_duration_snapshots * 10
            dur_str = f"{dur_sec:.0f}s" if dur_sec < 120 else f"{dur_sec/60:.1f}m"
            # Estimate daily profit at $1000 notional.
            hours = s.count * 10 / 3600
            if hours > 0:
                signals_per_day = s.count_above_2sigma / hours * 24
                est_daily = signals_per_day * s.mean_net_spread_at_2sigma / 100 * 1000
            else:
                signals_per_day = 0
                est_daily = 0
            print(
                f"{s.symbol:<14s} {pair:<16s} "
                f"{s.mean_net_spread_at_2sigma:>+9.4f}% {s.count_above_2sigma:>8d} "
                f"{dur_str:>7s} {est_daily:>+8.2f}$ "
                f"{s.mean_best_spread:>7.4f} {s.max_spread:>7.4f}"
            )

    print()

    # ── Overall distribution summary ──
    print("OVERALL SPREAD DISTRIBUTION")
    print("-" * 60)
    all_means = [s.mean_best_spread for s in stats]
    all_maxes = [s.max_spread for s in stats]
    if all_means:
        print(f"  Avg mean spread across all pairs: {sum(all_means)/len(all_means):.4f}%")
        print(f"  Avg max spread across all pairs:  {sum(all_maxes)/len(all_maxes):.4f}%")
        total_2s_signals = sum(s.count_above_2sigma for s in stats)
        total_profitable_signals = sum(
            s.count_above_2sigma for s in stats if s.mean_net_spread_at_2sigma > 0
        )
        print(f"  Total {sigma}σ+ signals: {total_2s_signals}")
        print(f"  Profitable signals (net > 0): {total_profitable_signals}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze spread snapshots")
    parser.add_argument(
        "--db", default="data/spread_arb.sqlite3",
        help="Path to SQLite database (default: data/spread_arb.sqlite3)",
    )
    parser.add_argument("--top", type=int, default=20, help="Number of top pairs to show")
    parser.add_argument("--sigma", type=float, default=2.0, help="Sigma threshold (default: 2.0)")
    parser.add_argument(
        "--fees", type=float, default=0.30,
        help="Estimated roundtrip cost %% (default: 0.30 = 2×taker both sides + buffer)",
    )
    args = parser.parse_args()

    db_path = args.db
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    print(f"Loading snapshots from {db_path}...")
    snapshots = load_snapshots(db_path)
    if not snapshots:
        print("No spread snapshots found. Run the scanner first to collect data.")
        sys.exit(0)

    print(f"Loaded {len(snapshots):,} snapshots")
    print()

    stats = compute_stats(snapshots, sigma_threshold=args.sigma, roundtrip_cost_pct=args.fees)
    print_report(stats, top_n=args.top, sigma=args.sigma)


if __name__ == "__main__":
    main()
