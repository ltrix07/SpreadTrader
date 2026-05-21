#!/usr/bin/env python3
"""
Live trading analytics dashboard.

Usage:
    python scripts/analyze_live.py --db data/spread_arb_proj.sqlite3
    python scripts/analyze_live.py --db data/spread_arb_proj.sqlite3 --since 2026-05-20
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True)
class Trade:
    symbol: str
    long_exchange: str
    short_exchange: str
    opened_at: datetime
    closed_at: datetime
    hold_seconds: float
    notional_usdt: float
    gross_pnl_usdt: float
    fees_usdt: float
    slippage_usdt: float
    net_pnl_usdt: float
    close_reason: str


@dataclass(slots=True)
class BalanceSnapshot:
    timestamp: datetime
    exchange: str
    total_usdt: float
    available_usdt: float
    snapshot_type: str


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fmt(v: float, signed: bool = True) -> str:
    return f"{v:+.2f}" if signed else f"{v:.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def _fmt_hold(s: float) -> str:
    if s < 60:
        return f"{int(round(s))}s"
    if s < 3600:
        return f"{int(round(s / 60))}m"
    return f"{s / 3600:.1f}h"


def _load_trades(conn: sqlite3.Connection, since: str | None) -> list[Trade]:
    where = []
    params: list[str] = []
    if since:
        where.append("opened_at >= ?")
        params.append(since)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT symbol, long_exchange, short_exchange, opened_at, closed_at,
               hold_seconds, notional_usdt, gross_pnl_usdt, fees_usdt,
               slippage_usdt, net_pnl_usdt, close_reason
        FROM paper_trades {where_sql} ORDER BY opened_at ASC
        """,
        tuple(params),
    ).fetchall()

    return [
        Trade(
            symbol=row["symbol"],
            long_exchange=row["long_exchange"],
            short_exchange=row["short_exchange"],
            opened_at=_parse_dt(row["opened_at"]),
            closed_at=_parse_dt(row["closed_at"]),
            hold_seconds=float(row["hold_seconds"]),
            notional_usdt=float(row["notional_usdt"] or 0),
            gross_pnl_usdt=float(row["gross_pnl_usdt"]),
            fees_usdt=float(row["fees_usdt"]),
            slippage_usdt=float(row["slippage_usdt"]),
            net_pnl_usdt=float(row["net_pnl_usdt"]),
            close_reason=row["close_reason"],
        )
        for row in rows
    ]


def _load_balance_snapshots(conn: sqlite3.Connection) -> list[BalanceSnapshot]:
    rows = conn.execute(
        "SELECT timestamp, exchange, total_usdt, available_usdt, snapshot_type "
        "FROM balance_snapshots ORDER BY timestamp ASC"
    ).fetchall()
    return [
        BalanceSnapshot(
            timestamp=_parse_dt(row["timestamp"]),
            exchange=row["exchange"],
            total_usdt=float(row["total_usdt"]),
            available_usdt=float(row["available_usdt"]),
            snapshot_type=row["snapshot_type"],
        )
        for row in rows
    ]


# ── Printers ──────────────────────────────────────────────

def print_balances(snapshots: list[BalanceSnapshot]) -> None:
    if not snapshots:
        print("\n  No balance snapshots found.")
        return

    # Group by exchange: first and last snapshot
    exchanges: dict[str, dict[str, BalanceSnapshot]] = {}
    for snap in snapshots:
        ex = exchanges.setdefault(snap.exchange, {})
        if "first" not in ex:
            ex["first"] = snap
        ex["last"] = snap

    total_start = 0.0
    total_current = 0.0

    print()
    print("BALANCE OVERVIEW")
    print("-" * 55)
    print(f"  {'Exchange':<12} {'Start':>10} {'Current':>10} {'Change':>10} {'Change%':>8}")
    print(f"  {'─' * 12} {'─' * 10} {'─' * 10} {'─' * 10} {'─' * 8}")

    for ex_name in sorted(exchanges):
        first = exchanges[ex_name]["first"]
        last = exchanges[ex_name]["last"]
        change = last.total_usdt - first.total_usdt
        pct = (change / first.total_usdt * 100) if first.total_usdt > 0 else 0
        total_start += first.total_usdt
        total_current += last.total_usdt
        print(
            f"  {ex_name:<12} ${first.total_usdt:>8.2f}  ${last.total_usdt:>8.2f}  "
            f"${_fmt(change):>8}  {_fmt_pct(pct):>7}"
        )

    total_change = total_current - total_start
    total_pct = (total_change / total_start * 100) if total_start > 0 else 0
    print(f"  {'─' * 12} {'─' * 10} {'─' * 10} {'─' * 10} {'─' * 8}")
    print(
        f"  {'TOTAL':<12} ${total_start:>8.2f}  ${total_current:>8.2f}  "
        f"${_fmt(total_change):>8}  {_fmt_pct(total_pct):>7}"
    )

    first_ts = min(s.timestamp for s in snapshots)
    last_ts = max(s.timestamp for s in snapshots)
    hours = max((last_ts - first_ts).total_seconds() / 3600, 0.01)
    print(f"\n  Tracking since: {first_ts:%Y-%m-%d %H:%M} ({hours:.1f} hours)")


def print_overall(trades: list[Trade]) -> None:
    total_net = sum(t.net_pnl_usdt for t in trades)
    total_gross = sum(t.gross_pnl_usdt for t in trades)
    total_fees = sum(t.fees_usdt for t in trades)
    total_slippage = sum(t.slippage_usdt for t in trades)

    start = min(t.opened_at for t in trades)
    end = max(t.closed_at for t in trades)
    hours = max((end - start).total_seconds() / 3600, 0.01)
    trades_per_hour = len(trades) / hours

    print()
    print("TRADE PERFORMANCE")
    print("-" * 55)
    print(f"  Period:         {start:%Y-%m-%d %H:%M} — {end:%Y-%m-%d %H:%M} ({hours:.1f}h)")
    print(f"  Total trades:   {len(trades)}  ({trades_per_hour:.1f}/hour)")
    print(f"  Net PnL:        ${_fmt(total_net)}")
    print(f"  Gross PnL:      ${_fmt(total_gross)}")
    print(f"  Total fees:     ${_fmt(-abs(total_fees))}")
    print(f"  Total slippage: ${_fmt(-abs(total_slippage))}")


def print_winloss(trades: list[Trade]) -> None:
    wins = [t for t in trades if t.net_pnl_usdt > 0]
    losses = [t for t in trades if t.net_pnl_usdt <= 0]

    wins_total = sum(t.net_pnl_usdt for t in wins)
    losses_total = sum(t.net_pnl_usdt for t in losses)

    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = wins_total / len(wins) if wins else 0
    avg_loss = losses_total / len(losses) if losses else 0

    if abs(losses_total) > 0:
        pf = wins_total / abs(losses_total)
    elif wins_total > 0:
        pf = math.inf
    else:
        pf = 0.0

    print()
    print("WIN / LOSS")
    print("-" * 55)
    print(f"  Wins:     {len(wins):>3}  ({win_rate:>5.1f}%)  avg: ${_fmt(avg_win)}  total: ${_fmt(wins_total)}")
    print(
        f"  Losses:   {len(losses):>3}  ({100 - win_rate:>5.1f}%)  "
        f"avg: ${_fmt(avg_loss)}  total: ${_fmt(losses_total)}"
    )
    pf_str = "inf" if math.isinf(pf) else f"{pf:.2f}"
    print(f"  Profit factor: {pf_str}")


def print_close_reasons(trades: list[Trade]) -> None:
    counts = Counter(t.close_reason for t in trades)
    net_by_reason: dict[str, float] = defaultdict(float)
    hold_by_reason: dict[str, float] = defaultdict(float)
    for t in trades:
        net_by_reason[t.close_reason] += t.net_pnl_usdt
        hold_by_reason[t.close_reason] += t.hold_seconds

    print()
    print("CLOSE REASONS")
    print("-" * 55)
    for reason in sorted(counts, key=lambda r: -counts[r]):
        c = counts[reason]
        pct = c / len(trades) * 100
        avg_pnl = net_by_reason[reason] / c
        avg_hold = hold_by_reason[reason] / c
        print(f"  {reason:<16} {c:>3} ({pct:>5.1f}%)  avg_pnl: ${_fmt(avg_pnl)}  avg_hold: {_fmt_hold(avg_hold)}")


def print_symbols(trades: list[Trade], top_n: int = 10) -> None:
    stats: dict[str, dict[str, float]] = {}
    for t in trades:
        s = stats.setdefault(t.symbol, {"count": 0, "net": 0, "wins": 0})
        s["count"] += 1
        s["net"] += t.net_pnl_usdt
        if t.net_pnl_usdt > 0:
            s["wins"] += 1

    ordered = sorted(stats.items(), key=lambda x: x[1]["net"], reverse=True)[:top_n]

    print()
    print(f"TOP {min(top_n, len(ordered))} SYMBOLS")
    print("-" * 55)
    for sym, s in ordered:
        c = int(s["count"])
        wr = s["wins"] / c * 100 if c else 0
        avg = s["net"] / c if c else 0
        print(f"  {sym:<14} {c:>3} trades  net: ${_fmt(s['net'])}  avg: ${_fmt(avg)}  wr: {wr:.0f}%")


def print_exchange_pairs(trades: list[Trade]) -> None:
    stats: dict[str, dict[str, float]] = {}
    for t in trades:
        pair = f"{t.long_exchange}->{t.short_exchange}"
        s = stats.setdefault(pair, {"count": 0, "net": 0})
        s["count"] += 1
        s["net"] += t.net_pnl_usdt

    ordered = sorted(stats.items(), key=lambda x: x[1]["net"], reverse=True)

    print()
    print("EXCHANGE PAIRS")
    print("-" * 55)
    for pair, s in ordered:
        c = int(s["count"])
        avg = s["net"] / c if c else 0
        print(f"  {pair:<18} {c:>3} trades  net: ${_fmt(s['net'])}  avg: ${_fmt(avg)}")


def print_recent_trades(trades: list[Trade], n: int = 10) -> None:
    recent = trades[-n:]

    print()
    print(f"LAST {len(recent)} TRADES")
    print("-" * 55)
    for t in recent:
        pair = f"{t.long_exchange}->{t.short_exchange}"
        print(
            f"  {t.closed_at:%m-%d %H:%M}  {t.symbol:<14} {pair:<16} "
            f"${_fmt(t.net_pnl_usdt)}  hold={_fmt_hold(t.hold_seconds)}  {t.close_reason}"
        )


def print_hourly_curve(trades: list[Trade]) -> None:
    hourly: dict[str, float] = defaultdict(float)
    for t in sorted(trades, key=lambda x: x.closed_at):
        key = t.closed_at.strftime("%Y-%m-%d %H:00")
        hourly[key] += t.net_pnl_usdt

    cum = 0.0
    print()
    print("EQUITY CURVE (hourly)")
    print("-" * 55)
    for hour in sorted(hourly):
        cum += hourly[hour]
        bar_len = int(abs(cum) * 10)  # scale
        bar = "█" * min(bar_len, 30)
        direction = "+" if cum >= 0 else "-"
        print(f"  {hour}  ${_fmt(cum):>8}  {direction}{bar}")


def print_bot_status(conn: sqlite3.Connection, snapshots: list[BalanceSnapshot]) -> None:
    # Count spread snapshots in last hour to verify bot is active
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM spread_snapshots WHERE timestamp > datetime('now', '-1 hour')"
    ).fetchone()
    recent_snapshots = row["cnt"] if row else 0

    # Count total spread snapshots
    row = conn.execute("SELECT COUNT(*) as cnt FROM spread_snapshots").fetchone()
    total_snapshots = row["cnt"] if row else 0

    # Latest balance snapshot time
    latest_ts = max(s.timestamp for s in snapshots) if snapshots else None

    print()
    print("BOT STATUS")
    print("-" * 55)
    print(f"  Spread snapshots total:     {total_snapshots:,}")
    print(f"  Spread snapshots (last 1h): {recent_snapshots:,}")
    if recent_snapshots > 0:
        print(f"  Bot status:                 ACTIVE")
    else:
        print(f"  Bot status:                 POSSIBLY STOPPED (no recent snapshots)")
    if latest_ts:
        print(f"  Last balance snapshot:      {latest_ts:%Y-%m-%d %H:%M}")


# ── Main ──────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Live trading analytics dashboard")
    parser.add_argument("--db", default="data/spread_arb_proj.sqlite3", help="Path to SQLite database")
    parser.add_argument("--since", help="Only include trades opened at/after this date")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print("=" * 55)
    print("       LIVE TRADING DASHBOARD")
    print("=" * 55)

    # Balance overview
    snapshots = _load_balance_snapshots(conn)
    print_balances(snapshots)

    # Bot status
    print_bot_status(conn, snapshots)

    # Trade analytics
    has_trades_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_trades' LIMIT 1"
    ).fetchone()

    if not has_trades_table:
        print("\n  No trades table found.")
        conn.close()
        return

    trades = _load_trades(conn, args.since)
    conn.close()

    if not trades:
        print()
        print("TRADES")
        print("-" * 55)
        print("  No trades yet. Bot is scanning for opportunities...")
        print("=" * 55)
        return

    print_overall(trades)
    print_winloss(trades)
    print_close_reasons(trades)
    print_symbols(trades)
    print_exchange_pairs(trades)
    print_recent_trades(trades)
    print_hourly_curve(trades)

    print()
    print("=" * 55)


if __name__ == "__main__":
    main()
