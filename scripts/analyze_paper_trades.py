from __future__ import annotations

import argparse
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MR_CLOSE_REASONS = ("mean_reversion", "stop_loss", "timeout", "stale_quote")


@dataclass(slots=True)
class Trade:
    symbol: str
    long_exchange: str
    short_exchange: str
    opened_at: datetime
    closed_at: datetime
    hold_seconds: float
    gross_pnl_usdt: float
    fees_usdt: float
    slippage_usdt: float
    funding_usdt: float
    net_pnl_usdt: float
    close_reason: str


def _parse_iso_datetime(value: str) -> datetime:
    # Handle both +00:00 and trailing Z.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze mean reversion paper trades")
    parser.add_argument("--db", default="data/spread_arb.sqlite3", help="Path to SQLite database")
    parser.add_argument("--since", help="Include trades opened at/after this ISO date or datetime")
    parser.add_argument("--notional", type=float, default=350.0, help="Notional per leg for return calculation")
    parser.add_argument(
        "--mr-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include only MR trade close reasons (default: true)",
    )
    return parser.parse_args()


def _fmt_money(value: float, *, signed: bool = True) -> str:
    return f"{value:+.2f}" if signed else f"{value:.2f}"


def _fmt_pct(value: float, *, signed: bool = True) -> str:
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def _fmt_hold(seconds: float) -> str:
    if seconds < 60:
        return f"{int(round(seconds))}s"
    if seconds < 3600:
        return f"{int(round(seconds / 60.0))}m"
    return f"{seconds / 3600.0:.1f}h"


def _load_trades(conn: sqlite3.Connection, *, since: str | None, mr_only: bool) -> list[Trade]:
    where: list[str] = []
    params: list[object] = []

    if mr_only:
        placeholders = ",".join("?" for _ in MR_CLOSE_REASONS)
        where.append(f"close_reason IN ({placeholders})")
        params.extend(MR_CLOSE_REASONS)

    if since:
        where.append("opened_at >= ?")
        params.append(since)

    where_sql = ""
    if where:
        where_sql = "WHERE " + " AND ".join(where)

    rows = conn.execute(
        f"""
        SELECT
            symbol,
            long_exchange,
            short_exchange,
            opened_at,
            closed_at,
            hold_seconds,
            gross_pnl_usdt,
            fees_usdt,
            slippage_usdt,
            funding_usdt,
            net_pnl_usdt,
            close_reason
        FROM paper_trades
        {where_sql}
        ORDER BY opened_at ASC
        """,
        tuple(params),
    ).fetchall()

    trades: list[Trade] = []
    for row in rows:
        trades.append(
            Trade(
                symbol=str(row["symbol"]),
                long_exchange=str(row["long_exchange"]),
                short_exchange=str(row["short_exchange"]),
                opened_at=_parse_iso_datetime(str(row["opened_at"])),
                closed_at=_parse_iso_datetime(str(row["closed_at"])),
                hold_seconds=float(row["hold_seconds"]),
                gross_pnl_usdt=float(row["gross_pnl_usdt"]),
                fees_usdt=float(row["fees_usdt"]),
                slippage_usdt=float(row["slippage_usdt"]),
                funding_usdt=float(row["funding_usdt"]),
                net_pnl_usdt=float(row["net_pnl_usdt"]),
                close_reason=str(row["close_reason"]),
            )
        )
    return trades


def _hold_bucket(hold_seconds: float) -> str:
    if hold_seconds < 10:
        return "< 10s"
    if hold_seconds < 60:
        return "10s-60s"
    if hold_seconds < 300:
        return "1m-5m"
    if hold_seconds < 900:
        return "5m-15m"
    return "> 15m"


def _print_header(trades: list[Trade], notional: float) -> None:
    start = min(t.opened_at for t in trades)
    end = max(t.closed_at for t in trades)
    hours = max((end - start).total_seconds() / 3600.0, 1e-9)

    print("=" * 64)
    print("MEAN REVERSION PAPER TRADING REPORT")
    print("=" * 64)
    print(f"Period: {start:%Y-%m-%d %H:%M} - {end:%Y-%m-%d %H:%M} ({hours:.1f} hours)")
    print(f"Total trades: {len(trades)}")
    print(f"Starting notional: ${notional:.2f}/leg (${2.0 * notional:.2f} total exposure)")


def _print_overall(trades: list[Trade], notional: float) -> None:
    total_net = sum(t.net_pnl_usdt for t in trades)
    total_gross = sum(t.gross_pnl_usdt for t in trades)
    total_fees = sum(t.fees_usdt for t in trades)
    total_slippage = sum(t.slippage_usdt for t in trades)

    start = min(t.opened_at for t in trades)
    end = max(t.closed_at for t in trades)
    hours = max((end - start).total_seconds() / 3600.0, 1e-9)

    capital = 2.0 * notional
    roc_pct = (total_net / capital) * 100.0 if capital > 0 else 0.0
    annualized_pct = roc_pct * (24.0 * 365.0 / hours)

    print()
    print("OVERALL PERFORMANCE")
    print("-" * 40)
    print(f"  Net PnL:           ${_fmt_money(total_net)}")
    print(f"  Gross PnL:         ${_fmt_money(total_gross)}")
    print(f"  Total fees:        ${_fmt_money(-abs(total_fees))}")
    print(f"  Total slippage:    ${_fmt_money(-abs(total_slippage))}")
    print(f"  Return on capital: {_fmt_pct(roc_pct)} (on ${capital:.2f})")
    print(f"  Annualized:        {_fmt_pct(annualized_pct)}")


def _print_win_loss(trades: list[Trade]) -> None:
    wins = [t for t in trades if t.net_pnl_usdt > 0]
    losses = [t for t in trades if t.net_pnl_usdt < 0]

    wins_total = sum(t.net_pnl_usdt for t in wins)
    losses_total = sum(t.net_pnl_usdt for t in losses)

    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    loss_rate = (len(losses) / len(trades) * 100.0) if trades else 0.0

    avg_win = (wins_total / len(wins)) if wins else 0.0
    avg_loss = (losses_total / len(losses)) if losses else 0.0

    gross_losses_abs = abs(losses_total)
    if gross_losses_abs > 0:
        profit_factor = wins_total / gross_losses_abs
    elif wins_total > 0:
        profit_factor = math.inf
    else:
        profit_factor = 0.0

    avg_win_loss_ratio = (avg_win / abs(avg_loss)) if avg_loss < 0 else math.inf if avg_win > 0 else 0.0

    print()
    print("WIN/LOSS BREAKDOWN")
    print("-" * 40)
    print(
        f"  Wins:   {len(wins):>3} ({win_rate:>5.1f}%)    "
        f"avg: ${_fmt_money(avg_win)}    total: ${_fmt_money(wins_total)}"
    )
    print(
        f"  Losses: {len(losses):>3} ({loss_rate:>5.1f}%)    "
        f"avg: ${_fmt_money(avg_loss)}    total: ${_fmt_money(losses_total)}"
    )

    if math.isinf(profit_factor):
        profit_factor_text = "inf"
    else:
        profit_factor_text = f"{profit_factor:.2f}"

    if math.isinf(avg_win_loss_ratio):
        ratio_text = "inf"
    else:
        ratio_text = f"{avg_win_loss_ratio:.2f}"

    print(f"  Profit factor: {profit_factor_text} (gross wins / gross losses)")
    print(f"  Avg win / avg loss ratio: {ratio_text}")


def _print_close_reasons(trades: list[Trade]) -> None:
    reason_counts = Counter(t.close_reason for t in trades)
    reason_net = defaultdict(float)
    reason_hold = defaultdict(float)

    for trade in trades:
        reason_net[trade.close_reason] += trade.net_pnl_usdt
        reason_hold[trade.close_reason] += trade.hold_seconds

    ordered_reasons = sorted(reason_counts.keys(), key=lambda reason: (-reason_counts[reason], reason))

    print()
    print("CLOSE REASONS")
    print("-" * 40)
    for reason in ordered_reasons:
        count = reason_counts[reason]
        pct = count / len(trades) * 100.0
        avg_pnl = reason_net[reason] / count
        avg_hold = reason_hold[reason] / count
        print(
            f"  {reason:<15} {count:>3} ({pct:>5.1f}%)  "
            f"avg_pnl: ${_fmt_money(avg_pnl)}  avg_hold: {_fmt_hold(avg_hold)}"
        )


def _print_hold_distribution(trades: list[Trade]) -> None:
    order = ["< 10s", "10s-60s", "1m-5m", "5m-15m", "> 15m"]
    bucket_counts = Counter()
    bucket_net = defaultdict(float)

    for trade in trades:
        bucket = _hold_bucket(trade.hold_seconds)
        bucket_counts[bucket] += 1
        bucket_net[bucket] += trade.net_pnl_usdt

    print()
    print("HOLD TIME DISTRIBUTION")
    print("-" * 40)
    for bucket in order:
        count = bucket_counts[bucket]
        if count == 0:
            print(f"  {bucket:<9} {count:>4} trades")
            continue
        avg_pnl = bucket_net[bucket] / count
        print(f"  {bucket:<9} {count:>4} trades   avg_pnl: ${_fmt_money(avg_pnl)}")


def _print_symbols(trades: list[Trade], top_n: int = 10) -> None:
    stats: dict[str, dict[str, float]] = {}

    for trade in trades:
        item = stats.setdefault(trade.symbol, {"count": 0.0, "net": 0.0, "wins": 0.0})
        item["count"] += 1
        item["net"] += trade.net_pnl_usdt
        if trade.net_pnl_usdt > 0:
            item["wins"] += 1

    ordered = sorted(stats.items(), key=lambda item: item[1]["net"], reverse=True)[:top_n]

    print()
    print("TOP SYMBOLS BY NET PNL")
    print("-" * 40)
    for symbol, item in ordered:
        count = int(item["count"])
        net = item["net"]
        avg = net / count if count else 0.0
        winrate = (item["wins"] / count * 100.0) if count else 0.0
        print(
            f"  {symbol:<10} {count:>3} trades  "
            f"net: ${_fmt_money(net)}  avg: ${_fmt_money(avg)}  winrate: {winrate:.1f}%"
        )


def _print_exchange_pairs(trades: list[Trade], top_n: int = 10) -> None:
    stats: dict[str, dict[str, float]] = {}

    for trade in trades:
        pair = f"{trade.long_exchange}->{trade.short_exchange}"
        item = stats.setdefault(pair, {"count": 0.0, "net": 0.0})
        item["count"] += 1
        item["net"] += trade.net_pnl_usdt

    ordered = sorted(stats.items(), key=lambda item: item[1]["net"], reverse=True)[:top_n]

    print()
    print("TOP EXCHANGE PAIRS BY NET PNL")
    print("-" * 40)
    for pair, item in ordered:
        count = int(item["count"])
        net = item["net"]
        avg = net / count if count else 0.0
        print(f"  {pair:<14} {count:>3} trades  net: ${_fmt_money(net)}  avg: ${_fmt_money(avg)}")


def _print_worst_trades(trades: list[Trade], top_n: int = 10) -> None:
    ordered = sorted(trades, key=lambda t: t.net_pnl_usdt)[:top_n]

    print()
    print("WORST TRADES")
    print("-" * 40)
    for index, trade in enumerate(ordered, start=1):
        pair = f"{trade.long_exchange}->{trade.short_exchange}"
        print(
            f"  #{index}: {trade.symbol} {pair:<12} | ${_fmt_money(trade.net_pnl_usdt)} | "
            f"hold={_fmt_hold(trade.hold_seconds)} | reason={trade.close_reason}"
        )


def _print_equity_curve(trades: list[Trade]) -> None:
    hourly_net: dict[datetime, float] = defaultdict(float)
    for trade in sorted(trades, key=lambda t: t.closed_at):
        hour = trade.closed_at.replace(minute=0, second=0, microsecond=0)
        hourly_net[hour] += trade.net_pnl_usdt

    cumulative = 0.0
    previous = 0.0
    ordered_hours = sorted(hourly_net.keys())

    print()
    print("EQUITY CURVE (hourly)")
    print("-" * 40)
    for hour in ordered_hours:
        cumulative += hourly_net[hour]
        marker = ""
        if cumulative > previous:
            marker = "  ^"
        elif cumulative < previous:
            marker = "  v"
        print(f"  {hour:%Y-%m-%d %H}:00  ${_fmt_money(cumulative)}{marker}")
        previous = cumulative


def main() -> None:
    args = _parse_args()
    db_path = Path(args.db)

    if args.notional <= 0:
        raise SystemExit("--notional must be > 0")

    if args.since:
        try:
            _parse_iso_datetime(args.since)
        except ValueError:
            try:
                datetime.fromisoformat(args.since)
            except ValueError as exc:
                raise SystemExit(f"Invalid --since value: {args.since!r}") from exc

    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_trades' LIMIT 1"
    ).fetchone()
    if table_exists is None:
        conn.close()
        raise SystemExit("Table 'paper_trades' does not exist")

    trades = _load_trades(conn, since=args.since, mr_only=bool(args.mr_only))
    conn.close()

    if not trades:
        print("No matching paper trades found for the selected filters.")
        return

    _print_header(trades, args.notional)
    _print_overall(trades, args.notional)
    _print_win_loss(trades)
    _print_close_reasons(trades)
    _print_hold_distribution(trades)
    _print_symbols(trades)
    _print_exchange_pairs(trades)
    _print_worst_trades(trades)
    _print_equity_curve(trades)


if __name__ == "__main__":
    main()
