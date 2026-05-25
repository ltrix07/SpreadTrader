#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Any

CONFIG_KEYS = [
    "mr_sigma_entry",
    "mr_min_net_edge_pct",
    "mr_revalidation_min_spread_pct",
    "mr_max_hold_seconds",
    "mr_max_bbo_spread_bps",
    "mr_take_profit_fraction",
    "mr_sigma_stop",
    "mr_min_stop_distance_pct",
    "mr_max_baseline_mean_pct",
]


def _to_float(row: dict[str, str], key: str) -> float:
    raw = row.get(key, "")
    if raw == "":
        return 0.0
    try:
        return float(raw)
    except ValueError:
        if raw.lower() == "inf":
            return math.inf
        if raw.lower() == "-inf":
            return -math.inf
        return 0.0


def _to_int(row: dict[str, str], key: str) -> int:
    raw = row.get(key, "")
    if raw == "":
        return 0
    try:
        return int(float(raw))
    except ValueError:
        return 0


def _fmt_num(value: float, digits: int = 2, signed: bool = True) -> str:
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    if signed:
        return f"{value:+.{digits}f}"
    return f"{value:.{digits}f}"


def _fmt_cfg(row: dict[str, str]) -> str:
    return (
        "{"
        + ", ".join(
            [
                f"sigma_entry:{row.get('mr_sigma_entry', '-')}",
                f"min_net_edge:{row.get('mr_min_net_edge_pct', '-')}",
                f"reval_spread:{row.get('mr_revalidation_min_spread_pct', '-')}",
                f"max_hold:{row.get('mr_max_hold_seconds', '-')}",
                f"max_bbo:{row.get('mr_max_bbo_spread_bps', '-')}",
                f"tp_frac:{row.get('mr_take_profit_fraction', '-')}",
                f"sigma_stop:{row.get('mr_sigma_stop', '-')}",
                f"max_mean:{row.get('mr_max_baseline_mean_pct', '-')}",
            ]
        )
        + "}"
    )


def _sorted_top(rows: list[dict[str, str]], key: str, *, top: int, min_trades: int = 0) -> list[dict[str, str]]:
    filtered = [row for row in rows if _to_int(row, "trade_count") >= min_trades]
    return sorted(filtered, key=lambda row: _to_float(row, key), reverse=True)[:top]


def _print_top_block(rows: list[dict[str, str]], title: str, key: str, *, top: int, min_trades: int = 0) -> None:
    print(title)
    print("-" * 55)
    top_rows = _sorted_top(rows, key, top=top, min_trades=min_trades)
    if not top_rows:
        print("  No runs")
        print()
        return
    for idx, row in enumerate(top_rows, start=1):
        print(
            f"  #{idx:<2} score={_fmt_num(_to_float(row, 'score')):<8} "
            f"pnl={_fmt_num(_to_float(row, 'total_net_pnl_usdt')):<8} "
            f"wr={_fmt_num(_to_float(row, 'winrate'), 2, signed=False):<5} "
            f"trades={_to_int(row, 'trade_count'):<5} {_fmt_cfg(row)}"
        )
    print()


def _print_sensitivity(rows: list[dict[str, str]]) -> None:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        sweep_name = row.get("sweep_name", "")
        if not sweep_name or sweep_name == "baseline":
            continue
        grouped.setdefault(sweep_name, []).append(row)

    for sweep_name in sorted(grouped):
        variants = grouped[sweep_name]
        variants.sort(key=lambda row: _to_float(row, "param_value"))
        best = max(variants, key=lambda row: _to_float(row, "score"))
        print(f"SENSITIVITY: {sweep_name}")
        print("-" * 55)
        for row in variants:
            marker = " <- BEST" if row is best else ""
            print(
                f"  value={row.get('param_value', '-'):>6}  "
                f"score={_fmt_num(_to_float(row, 'score')):<8}  "
                f"pnl={_fmt_num(_to_float(row, 'total_net_pnl_usdt')):<8}  "
                f"wr={_fmt_num(_to_float(row, 'winrate'), 2, signed=False):<5}  "
                f"trades={_to_int(row, 'trade_count'):<5}{marker}"
            )
        print()


def _print_best_details(best: dict[str, str]) -> None:
    print("BEST CONFIGURATION")
    print("-" * 55)
    for key in CONFIG_KEYS:
        print(f"  {key:<32} {best.get(key, '-')}")

    print()
    print(f"  Total trades:    {_to_int(best, 'trade_count')}")
    print(f"  Net PnL:         {_fmt_num(_to_float(best, 'total_net_pnl_usdt'))} USDT")
    print(f"  Winrate:         {_fmt_num(_to_float(best, 'winrate') * 100.0, 1, signed=False)}%")
    print(f"  Profit factor:   {_fmt_num(_to_float(best, 'profit_factor'), 2, signed=False)}")
    print(f"  Sharpe-like:     {_fmt_num(_to_float(best, 'sharpe_like'), 2, signed=False)}")
    print(f"  Max drawdown:    {_fmt_num(_to_float(best, 'max_drawdown_usdt'))} USDT")
    print()
    print("  By close reason:")

    trade_count = max(_to_int(best, "trade_count"), 1)
    reasons = [
        ("mean_reversion", "mean_reversion_count", "mean_reversion_wr", "mean_reversion_avg_pnl"),
        ("timeout", "timeout_count", "timeout_wr", "timeout_avg_pnl"),
        ("stop_loss", "stop_loss_count", "stop_loss_wr", "stop_loss_avg_pnl"),
    ]
    for reason, count_key, wr_key, avg_key in reasons:
        count = _to_int(best, count_key)
        pct = count / trade_count * 100.0
        wr = _to_float(best, wr_key)
        avg = _to_float(best, avg_key)
        print(
            f"    {reason:<14} {count:>4} ({pct:>5.1f}%)  "
            f"wr={_fmt_num(wr, 2, signed=False):<5} avg={_fmt_num(avg)}"
        )

    stale_count = _to_int(best, "stale_quote_count")
    timeout_loss_count = _to_int(best, "timeout_loss_count")
    print(f"    {'stale_quote':<14} {stale_count:>4} ({stale_count / trade_count * 100.0:>5.1f}%)")
    print(f"    {'timeout_loss':<14} {timeout_loss_count:>4} ({timeout_loss_count / trade_count * 100.0:>5.1f}%)")
    print()


def _load_optional_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _print_breakdown(title: str, rows: list[dict[str, str]], key_col: str, *, top: int = 15) -> None:
    if not rows:
        return
    print(title)
    print("-" * 55)
    ranked = sorted(rows, key=lambda row: _to_float(row, "total_net_pnl_usdt"), reverse=True)[:top]
    for row in ranked:
        print(
            f"  {row.get(key_col, '-'):>10}  trades={_to_int(row, 'trade_count'):<4}  "
            f"wr={_fmt_num(_to_float(row, 'winrate'), 2, signed=False):<5}  "
            f"net={_fmt_num(_to_float(row, 'total_net_pnl_usdt'))}"
        )
    print()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render text report from backtest CSV")
    parser.add_argument("csv_path", help="Path to backtest_results.csv")
    parser.add_argument("--top", type=int, default=5, help="Top N rows in ranking blocks")
    parser.add_argument("--best-only", action="store_true", help="Show only best configuration details")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        raise SystemExit(f"Results file not found: {csv_path}")

    with csv_path.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        raise SystemExit("No rows in results CSV")

    best = max(rows, key=lambda row: _to_float(row, "score"))

    print("=" * 55)
    print("              BACKTEST REPORT")
    print("=" * 55)
    print()

    if not args.best_only:
        _print_top_block(rows, f"TOP {args.top} BY SCORE (composite PnL x WR)", "score", top=args.top)
        _print_top_block(rows, f"TOP {args.top} BY TOTAL PNL", "total_net_pnl_usdt", top=args.top)
        _print_top_block(rows, f"TOP {args.top} BY WINRATE (min 20 trades)", "winrate", top=args.top, min_trades=20)
        _print_top_block(rows, f"TOP {args.top} BY PROFIT FACTOR (min 20 trades)", "profit_factor", top=args.top, min_trades=20)
        _print_sensitivity(rows)

    _print_best_details(best)

    base_dir = csv_path.parent
    by_symbol = _load_optional_csv(base_dir / "backtest_best_by_symbol.csv")
    by_pair = _load_optional_csv(base_dir / "backtest_best_by_pair.csv")
    by_hour = _load_optional_csv(base_dir / "backtest_best_by_hour.csv")
    _print_breakdown("BEST BREAKDOWN: SYMBOL", by_symbol, "symbol", top=15)
    _print_breakdown("BEST BREAKDOWN: PAIR", by_pair, "pair", top=15)
    _print_breakdown("BEST BREAKDOWN: HOUR UTC", by_hour, "hour_utc", top=15)


if __name__ == "__main__":
    main()
