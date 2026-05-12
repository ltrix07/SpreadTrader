#!/usr/bin/env python3
"""Parse a smoke-test log file and print a compact summary.

Usage:
    python scripts/summarize_log.py smoke_test.log
    python scripts/summarize_log.py smoke_test.log --top 20
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


# ── regex patterns ──────────────────────────────────────────────────
RE_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
RE_OBSERVED = re.compile(
    r"observed \| (\S+) \| long=(\w+) ask=([\d.]+) \| short=(\w+) bid=([\d.]+) "
    r"\| raw=([\d.+-]+)% \| net=([\d.+-]+)% \| cost=([\d.]+)%"
)
RE_REJECTED = re.compile(
    r"rejected \| (\S+) \| long=(\w+) short=(\w+) \| raw=([\d.+-]+)% \| net=([\d.+-]+)% \| reason=(\S+)"
)
RE_WARNING = re.compile(r"\| WARNING \|.*?(\S+)$")
RE_FETCH_ERROR = re.compile(r"fetch error for (\S+): (.+)$")
RE_PAPER_OPEN = re.compile(
    r"paper open \| (\S+) \| long=(\w+) @ ([\d.]+) \| short=(\w+) @ ([\d.]+) "
    r"\| raw=([\d.+-]+)% \| net=([\d.+-]+)%"
)
RE_PAPER_CLOSE = re.compile(
    r"paper close \| (\S+) \| reason=(\S+) \| hold=([\d.]+)s "
    r"\| net_pnl=([\d.+-]+) USDT"
)
RE_PAPER_MISSED = re.compile(
    r"paper missed \| (\S+) \| long=(\w+) short=(\w+) \| reason=(\S+)"
)
RE_TOP_SPREAD_LINE = re.compile(
    r"^\s+(\S+)\s+(\S+)->(\S+)\s+raw=([+\-\d.]+)%\s+net=([+\-\d.]+)%"
)
RE_CONFIG = re.compile(r"effective config \| exchanges=\[([^\]]*)\] \| symbols=\[([^\]]*)\]")


@dataclass
class Stats:
    first_ts: str = ""
    last_ts: str = ""
    exchanges: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    total_lines: int = 0

    # Observations
    observed_count: int = 0
    rejected_count: int = 0
    rejection_reasons: Counter = field(default_factory=Counter)

    # Raw spreads seen (symbol -> max raw spread %)
    max_raw_by_symbol: dict[str, float] = field(default_factory=lambda: defaultdict(lambda: -999.0))
    # Best raw spread per exchange pair
    max_raw_by_pair: dict[str, float] = field(default_factory=lambda: defaultdict(lambda: -999.0))

    # All raw spread values for histogram
    all_raw_spreads: list[float] = field(default_factory=list)

    # Errors
    fetch_errors: Counter = field(default_factory=Counter)  # "exchange:symbol" -> count
    warning_count: int = 0

    # Paper trading
    paper_opens: int = 0
    paper_closes: int = 0
    paper_missed: int = 0
    paper_missed_reasons: Counter = field(default_factory=Counter)
    paper_close_reasons: Counter = field(default_factory=Counter)
    paper_pnl_list: list[float] = field(default_factory=list)


def parse_log(path: Path) -> Stats:
    stats = Stats()

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            stats.total_lines += 1
            line = line.rstrip()

            # Track time range.
            ts_match = RE_TIMESTAMP.match(line)
            if ts_match:
                ts = ts_match.group(1)
                if not stats.first_ts:
                    stats.first_ts = ts
                stats.last_ts = ts

            # Config line.
            cfg = RE_CONFIG.search(line)
            if cfg:
                stats.exchanges = [e.strip().strip("'\"") for e in cfg.group(1).split(",")]
                stats.symbols = [s.strip().strip("'\"") for s in cfg.group(2).split(",")]
                continue

            # Observed opportunity.
            m = RE_OBSERVED.search(line)
            if m:
                stats.observed_count += 1
                symbol = m.group(1)
                long_ex = m.group(2)
                short_ex = m.group(4)
                raw = float(m.group(6))
                stats.all_raw_spreads.append(raw)
                pair = f"{long_ex}->{short_ex}"
                if raw > stats.max_raw_by_symbol[symbol]:
                    stats.max_raw_by_symbol[symbol] = raw
                if raw > stats.max_raw_by_pair[pair]:
                    stats.max_raw_by_pair[pair] = raw
                continue

            # Rejected opportunity.
            m = RE_REJECTED.search(line)
            if m:
                stats.rejected_count += 1
                symbol = m.group(1)
                long_ex = m.group(2)
                short_ex = m.group(3)
                raw = float(m.group(4))
                reason = m.group(6)
                stats.rejection_reasons[reason] += 1
                stats.all_raw_spreads.append(raw)
                pair = f"{long_ex}->{short_ex}"
                if raw > stats.max_raw_by_symbol[symbol]:
                    stats.max_raw_by_symbol[symbol] = raw
                if raw > stats.max_raw_by_pair[pair]:
                    stats.max_raw_by_pair[pair] = raw
                continue

            # Fetch error / warning.
            m = RE_FETCH_ERROR.search(line)
            if m:
                stats.warning_count += 1
                symbol = m.group(1)
                error_msg = m.group(2)
                # Extract exchange from the module path.
                ex_match = re.search(r"exchanges\.base\.(\w+)", line)
                ex_name = ex_match.group(1) if ex_match else "unknown"
                short_error = error_msg[:60]
                stats.fetch_errors[f"{ex_name}:{symbol} ({short_error})"] += 1
                continue

            if "| WARNING |" in line:
                stats.warning_count += 1

            # Paper engine.
            m = RE_PAPER_OPEN.search(line)
            if m:
                stats.paper_opens += 1
                continue

            m = RE_PAPER_CLOSE.search(line)
            if m:
                stats.paper_closes += 1
                reason = m.group(2)
                pnl = float(m.group(4))
                stats.paper_close_reasons[reason] += 1
                stats.paper_pnl_list.append(pnl)
                continue

            m = RE_PAPER_MISSED.search(line)
            if m:
                stats.paper_missed += 1
                reason = m.group(4)
                stats.paper_missed_reasons[reason] += 1
                continue

    return stats


def spread_histogram(values: list[float]) -> str:
    """Simple text histogram of raw spread distribution."""
    if not values:
        return "  (no data)"

    buckets = [
        ("< -0.10%", lambda v: v < -0.10),
        ("-0.10..0%", lambda v: -0.10 <= v < 0),
        ("  0..0.02%", lambda v: 0 <= v < 0.02),
        ("0.02..0.05%", lambda v: 0.02 <= v < 0.05),
        ("0.05..0.10%", lambda v: 0.05 <= v < 0.10),
        ("0.10..0.20%", lambda v: 0.10 <= v < 0.20),
        ("0.20..0.50%", lambda v: 0.20 <= v < 0.50),
        (">= 0.50%", lambda v: v >= 0.50),
    ]

    counts = []
    for label, pred in buckets:
        counts.append((label, sum(1 for v in values if pred(v))))

    max_count = max(c for _, c in counts) if counts else 1
    lines = []
    for label, count in counts:
        bar_len = int(count / max_count * 30) if max_count > 0 else 0
        pct = count / len(values) * 100 if values else 0
        lines.append(f"  {label:>12s} | {'█' * bar_len:<30s} {count:>6d} ({pct:5.1f}%)")
    return "\n".join(lines)


def print_summary(stats: Stats, top_n: int = 10) -> None:
    print("=" * 65)
    print("  SMOKE TEST LOG SUMMARY")
    print("=" * 65)

    print(f"\nTime range : {stats.first_ts} → {stats.last_ts}")
    print(f"Lines      : {stats.total_lines}")
    print(f"Exchanges  : {', '.join(stats.exchanges)} ({len(stats.exchanges)})")
    print(f"Symbols    : {len(stats.symbols)}")

    # ── Errors ──
    print(f"\n── ERRORS & WARNINGS ({stats.warning_count}) ──")
    if stats.fetch_errors:
        for key, count in stats.fetch_errors.most_common(10):
            print(f"  {count:>4d}x  {key}")
    else:
        print("  (none)")

    # ── Spread overview ──
    total_checks = stats.observed_count + stats.rejected_count
    print(f"\n── SPREAD CHECKS ({total_checks}) ──")
    print(f"  Observed : {stats.observed_count}")
    print(f"  Rejected : {stats.rejected_count}")
    if stats.rejection_reasons:
        for reason, count in stats.rejection_reasons.most_common():
            print(f"    {reason:<35s} {count:>6d}")

    # ── Raw spread distribution ──
    if stats.all_raw_spreads:
        spreads = stats.all_raw_spreads
        print(f"\n── RAW SPREAD DISTRIBUTION (n={len(spreads)}) ──")
        print(f"  min={min(spreads):+.4f}%  median={sorted(spreads)[len(spreads)//2]:+.4f}%  "
              f"max={max(spreads):+.4f}%  mean={sum(spreads)/len(spreads):+.4f}%")
        print(spread_histogram(spreads))

    # ── Top spreads by symbol ──
    print(f"\n── TOP {top_n} SYMBOLS BY MAX RAW SPREAD ──")
    sorted_symbols = sorted(stats.max_raw_by_symbol.items(), key=lambda x: x[1], reverse=True)[:top_n]
    for symbol, raw in sorted_symbols:
        print(f"  {symbol:<16s}  max_raw={raw:+.4f}%")

    # ── Top spreads by exchange pair ──
    print(f"\n── TOP {top_n} EXCHANGE PAIRS BY MAX RAW SPREAD ──")
    sorted_pairs = sorted(stats.max_raw_by_pair.items(), key=lambda x: x[1], reverse=True)[:top_n]
    for pair, raw in sorted_pairs:
        print(f"  {pair:<20s}  max_raw={raw:+.4f}%")

    # ── Paper trading ──
    print(f"\n── PAPER TRADING ──")
    print(f"  Opens  : {stats.paper_opens}")
    print(f"  Closes : {stats.paper_closes}")
    print(f"  Missed : {stats.paper_missed}")

    if stats.paper_pnl_list:
        pnl = stats.paper_pnl_list
        wins = sum(1 for p in pnl if p > 0)
        total_pnl = sum(pnl)
        avg_pnl = total_pnl / len(pnl)
        winrate = wins / len(pnl) * 100
        print(f"  Total PnL  : {total_pnl:+.4f} USDT")
        print(f"  Avg PnL    : {avg_pnl:+.4f} USDT")
        print(f"  Winrate    : {winrate:.1f}% ({wins}/{len(pnl)})")
        print(f"  Best trade : {max(pnl):+.4f} USDT")
        print(f"  Worst trade: {min(pnl):+.4f} USDT")

    if stats.paper_close_reasons:
        print("  Close reasons:")
        for reason, count in stats.paper_close_reasons.most_common():
            print(f"    {reason:<24s} {count:>4d}")

    if stats.paper_missed_reasons:
        print("  Missed reasons:")
        for reason, count in stats.paper_missed_reasons.most_common():
            print(f"    {reason:<35s} {count:>4d}")

    print("\n" + "=" * 65)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize spread-arb smoke test logs")
    parser.add_argument("logfile", help="Path to the log file")
    parser.add_argument("--top", type=int, default=10, help="Number of top items to show (default: 10)")
    args = parser.parse_args()

    path = Path(args.logfile)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    stats = parse_log(path)
    print_summary(stats, top_n=args.top)


if __name__ == "__main__":
    main()
