#!/usr/bin/env python3
"""Audit spread snapshot pipeline for bias and methodological issues.

This script is intentionally separate from scripts/analyze_spreads.py so the legacy
report remains reproducible while we validate assumptions with stricter methodology.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path


EPS = 1e-9


@dataclass
class DirectionStats:
    symbol: str
    long_exchange: str
    short_exchange: str
    n: int
    mean_spread: float
    std_spread_sample: float
    rolling_signals: int
    rolling_signal_rate_pct: float
    mean_entry_spread_at_signal: float
    mean_expected_edge_to_mean_pct: float
    mean_net_edge_pct: float


def percentile_sorted(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if p <= 0:
        return sorted_values[0]
    if p >= 1:
        return sorted_values[-1]
    rank = (len(sorted_values) - 1) * p
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return sorted_values[lo]
    w = rank - lo
    return sorted_values[lo] * (1 - w) + sorted_values[hi] * w


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def sample_std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mu = mean(values)
    var = sum((x - mu) ** 2 for x in values) / (n - 1)
    return math.sqrt(max(var, 0.0))


def load_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT id, timestamp, symbol, exchange_a, exchange_b,
               bid_a, ask_a, bid_b, ask_b,
               raw_spread_ab_pct, raw_spread_ba_pct, best_raw_spread_pct,
               quote_age_a_ms, quote_age_b_ms
        FROM spread_snapshots
        ORDER BY timestamp, id
        """
    ).fetchall()


def check_storage_integrity(conn: sqlite3.Connection) -> dict[str, float]:
    dup_rows = conn.execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT timestamp, symbol, exchange_a, exchange_b, COUNT(*) AS c
            FROM spread_snapshots
            GROUP BY timestamp, symbol, exchange_a, exchange_b
            HAVING c > 1
        )
        """
    ).fetchone()[0]

    out_of_order = conn.execute(
        """
        SELECT COUNT(*)
        FROM spread_snapshots
        WHERE exchange_a > exchange_b
        """
    ).fetchone()[0]

    non_positive = conn.execute(
        """
        SELECT COUNT(*)
        FROM spread_snapshots
        WHERE ask_a <= 0 OR ask_b <= 0 OR bid_a <= 0 OR bid_b <= 0
        """
    ).fetchone()[0]

    return {
        "duplicate_group_count": float(dup_rows),
        "non_canonical_exchange_order_rows": float(out_of_order),
        "non_positive_quote_rows": float(non_positive),
    }


def verify_spread_formula(rows: list[sqlite3.Row]) -> dict[str, float]:
    err_ab = 0
    err_ba = 0
    err_best = 0

    for r in rows:
        ask_a = float(r["ask_a"])
        ask_b = float(r["ask_b"])
        bid_a = float(r["bid_a"])
        bid_b = float(r["bid_b"])

        calc_ab = (bid_b - ask_a) / ask_a * 100.0 if ask_a > 0 else 0.0
        calc_ba = (bid_a - ask_b) / ask_b * 100.0 if ask_b > 0 else 0.0
        calc_best = max(calc_ab, calc_ba)

        if abs(calc_ab - float(r["raw_spread_ab_pct"])) > EPS:
            err_ab += 1
        if abs(calc_ba - float(r["raw_spread_ba_pct"])) > EPS:
            err_ba += 1
        if abs(calc_best - float(r["best_raw_spread_pct"])) > EPS:
            err_best += 1

    n = len(rows)
    return {
        "formula_mismatch_ab": float(err_ab),
        "formula_mismatch_ba": float(err_ba),
        "formula_mismatch_best": float(err_best),
        "formula_mismatch_ab_pct": (err_ab / n * 100.0) if n else 0.0,
        "formula_mismatch_ba_pct": (err_ba / n * 100.0) if n else 0.0,
        "formula_mismatch_best_pct": (err_best / n * 100.0) if n else 0.0,
    }


def compute_max_selection_bias(rows: list[sqlite3.Row]) -> dict[str, float]:
    biases = []
    stale_2s = 0
    stale_30s = 0
    for r in rows:
        ab = float(r["raw_spread_ab_pct"])
        ba = float(r["raw_spread_ba_pct"])
        best = float(r["best_raw_spread_pct"])
        avg_dir = 0.5 * (ab + ba)
        biases.append(best - avg_dir)

        if max(float(r["quote_age_a_ms"]), float(r["quote_age_b_ms"])) > 2_000:
            stale_2s += 1
        if max(float(r["quote_age_a_ms"]), float(r["quote_age_b_ms"])) > 30_000:
            stale_30s += 1

    sorted_bias = sorted(biases)
    return {
        "mean_max_bias_pct": mean(biases),
        "median_max_bias_pct": percentile_sorted(sorted_bias, 0.5),
        "p95_max_bias_pct": percentile_sorted(sorted_bias, 0.95),
        "p99_max_bias_pct": percentile_sorted(sorted_bias, 0.99),
        "stale_gt_2s_pct": (stale_2s / len(rows) * 100.0) if rows else 0.0,
        "stale_gt_30s_pct": (stale_30s / len(rows) * 100.0) if rows else 0.0,
    }


def fee_map_from_args(args: argparse.Namespace) -> dict[str, float]:
    return {
        "binance": args.fee_binance,
        "bybit": args.fee_bybit,
        "okx": args.fee_okx,
        "bitget": args.fee_bitget,
        "gate": args.fee_gate,
        "htx": args.fee_htx,
        "mexc": args.fee_mexc,
    }


def roundtrip_cost(long_ex: str, short_ex: str, fees: dict[str, float], extra_cost_pct: float) -> float:
    fee_l = fees.get(long_ex, 0.05)
    fee_s = fees.get(short_ex, 0.05)
    return 2.0 * (fee_l + fee_s) + extra_cost_pct


def build_directional_groups(rows: list[sqlite3.Row]) -> dict[tuple[str, str, str], list[float]]:
    groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for r in rows:
        symbol = str(r["symbol"])
        ex_a = str(r["exchange_a"])
        ex_b = str(r["exchange_b"])

        groups[(symbol, ex_a, ex_b)].append(float(r["raw_spread_ab_pct"]))
        groups[(symbol, ex_b, ex_a)].append(float(r["raw_spread_ba_pct"]))

    return groups


def compute_directional_stats(
    rows: list[sqlite3.Row],
    sigma: float,
    rolling_window: int,
    extra_cost_pct: float,
    fees: dict[str, float],
    min_samples: int,
) -> list[DirectionStats]:
    groups = build_directional_groups(rows)
    out: list[DirectionStats] = []

    for (symbol, long_ex, short_ex), spreads in groups.items():
        n = len(spreads)
        if n < min_samples:
            continue

        mu = mean(spreads)
        sd = sample_std(spreads)

        window = deque()
        s = 0.0
        s2 = 0.0
        signals = 0
        signal_spreads: list[float] = []
        expected_edges: list[float] = []
        net_edges: list[float] = []
        fee_cost = roundtrip_cost(long_ex, short_ex, fees, extra_cost_pct)

        for x in spreads:
            if len(window) >= rolling_window:
                win_n = len(window)
                win_mean = s / win_n
                raw_var = (s2 - (s * s) / win_n) / (win_n - 1) if win_n > 1 else 0.0
                win_std = math.sqrt(max(raw_var, 0.0))
                threshold = win_mean + sigma * win_std
                if x > threshold and win_std > 0:
                    signals += 1
                    edge_to_mean = x - win_mean
                    signal_spreads.append(x)
                    expected_edges.append(edge_to_mean)
                    net_edges.append(edge_to_mean - fee_cost)

            window.append(x)
            s += x
            s2 += x * x
            if len(window) > rolling_window:
                old = window.popleft()
                s -= old
                s2 -= old * old

        out.append(DirectionStats(
            symbol=symbol,
            long_exchange=long_ex,
            short_exchange=short_ex,
            n=n,
            mean_spread=mu,
            std_spread_sample=sd,
            rolling_signals=signals,
            rolling_signal_rate_pct=(signals / n * 100.0) if n else 0.0,
            mean_entry_spread_at_signal=mean(signal_spreads),
            mean_expected_edge_to_mean_pct=mean(expected_edges),
            mean_net_edge_pct=mean(net_edges),
        ))

    return out


def estimate_symbol_dedup_signals(rows: list[sqlite3.Row], sigma: float) -> dict[str, float]:
    by_pair: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    by_symbol_ts_hits: dict[tuple[str, str], int] = defaultdict(int)

    for r in rows:
        key = (str(r["symbol"]), str(r["exchange_a"]), str(r["exchange_b"]))
        by_pair[key].append(float(r["best_raw_spread_pct"]))

    thresholds: dict[tuple[str, str, str], float] = {}
    for key, values in by_pair.items():
        mu = mean(values)
        sd = sample_std(values)
        thresholds[key] = mu + sigma * sd

    total_pair_hits = 0
    for r in rows:
        symbol = str(r["symbol"])
        ts = str(r["timestamp"])
        key = (symbol, str(r["exchange_a"]), str(r["exchange_b"]))
        spread = float(r["best_raw_spread_pct"])
        if spread > thresholds.get(key, float("inf")):
            total_pair_hits += 1
            by_symbol_ts_hits[(symbol, ts)] += 1

    unique_symbol_hits = len(by_symbol_ts_hits)
    inflation = (total_pair_hits / unique_symbol_hits) if unique_symbol_hits else 0.0
    return {
        "pair_level_hits": float(total_pair_hits),
        "symbol_timestamp_unique_hits": float(unique_symbol_hits),
        "pair_hit_inflation_vs_symbol_hit": inflation,
    }


def print_audit(
    rows: list[sqlite3.Row],
    integrity: dict[str, float],
    formula: dict[str, float],
    max_bias: dict[str, float],
    directional: list[DirectionStats],
    symbol_dedup: dict[str, float],
) -> None:
    print("=" * 110)
    print("SPREAD PIPELINE AUDIT")
    print("=" * 110)
    print(f"rows: {len(rows):,}")
    print()

    print("[integrity]")
    for k, v in integrity.items():
        print(f"  {k}: {v:.0f}")
    print()

    print("[formula-check]")
    for k, v in formula.items():
        if k.endswith("_pct"):
            print(f"  {k}: {v:.6f}%")
        else:
            print(f"  {k}: {v:.0f}")
    print()

    print("[max-selection-bias]")
    for k, v in max_bias.items():
        if k.endswith("_pct"):
            print(f"  {k}: {v:.6f}%")
        else:
            print(f"  {k}: {v:.4f}")
    print()

    print("[signal-overlap]")
    for k, v in symbol_dedup.items():
        if "inflation" in k:
            print(f"  {k}: {v:.3f}x")
        else:
            print(f"  {k}: {v:.0f}")
    print()

    profitable = [d for d in directional if d.mean_net_edge_pct > 0 and d.rolling_signals >= 3]
    profitable.sort(key=lambda x: x.mean_net_edge_pct * x.rolling_signals, reverse=True)

    print("[top-directional-net-edge]")
    print(
        f"{'Symbol':<14} {'Direction':<17} {'N':>6} {'Signals':>8} {'Sig%':>7} "
        f"{'Entry@Sig':>10} {'Edge->Mean':>11} {'NetEdge':>9}"
    )
    for d in profitable[:20]:
        direction = f"{d.long_exchange}->{d.short_exchange}"
        print(
            f"{d.symbol:<14} {direction:<17} {d.n:>6d} {d.rolling_signals:>8d} "
            f"{d.rolling_signal_rate_pct:>6.2f}% {d.mean_entry_spread_at_signal:>9.4f}% "
            f"{d.mean_expected_edge_to_mean_pct:>10.4f}% {d.mean_net_edge_pct:>+8.4f}%"
        )



def main() -> None:
    parser = argparse.ArgumentParser(description="Audit spread snapshot pipeline")
    parser.add_argument("--db", default="data/spread_arb.sqlite3", help="SQLite path")
    parser.add_argument("--sigma", type=float, default=2.0, help="Signal threshold multiplier")
    parser.add_argument("--rolling-window", type=int, default=360, help="Past samples in rolling baseline")
    parser.add_argument("--min-samples", type=int, default=400, help="Min observations per direction")
    parser.add_argument("--extra-cost", type=float, default=0.10, help="Slippage+safety cost pct per roundtrip")

    parser.add_argument("--fee-binance", type=float, default=0.05)
    parser.add_argument("--fee-bybit", type=float, default=0.055)
    parser.add_argument("--fee-okx", type=float, default=0.05)
    parser.add_argument("--fee-bitget", type=float, default=0.05)
    parser.add_argument("--fee-gate", type=float, default=0.05)
    parser.add_argument("--fee-htx", type=float, default=0.05)
    parser.add_argument("--fee-mexc", type=float, default=0.05)

    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='spread_snapshots'"
        ).fetchone()
        if not has_table:
            print("Table spread_snapshots not found in database.")
            sys.exit(2)

        rows = load_rows(conn)
        if not rows:
            print("Table spread_snapshots is empty.")
            sys.exit(0)

        integrity = check_storage_integrity(conn)
        formula = verify_spread_formula(rows)
        max_bias = compute_max_selection_bias(rows)
        fees = fee_map_from_args(args)
        directional = compute_directional_stats(
            rows=rows,
            sigma=args.sigma,
            rolling_window=args.rolling_window,
            extra_cost_pct=args.extra_cost,
            fees=fees,
            min_samples=args.min_samples,
        )
        symbol_dedup = estimate_symbol_dedup_signals(rows, sigma=args.sigma)

        print_audit(rows, integrity, formula, max_bias, directional, symbol_dedup)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
