#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from spread_arb.config import Settings
from spread_arb.mean_reversion_engine import MeanReversionEngine
from spread_arb.models import ExchangeName, Quote
from spread_arb.storage import PaperTradeRecord

BASE_PARAMS: dict[str, float | int] = {
    "mr_sigma_entry": 2.5,
    "mr_min_net_edge_pct": 0.30,
    "mr_revalidation_min_spread_pct": 0.60,
    "mr_max_hold_seconds": 300,
    "mr_max_bbo_spread_bps": 8.0,
    "mr_max_baseline_mean_pct": 0.50,
    "mr_take_profit_fraction": 0.75,
    "mr_sigma_stop": 6.0,
    "mr_min_stop_distance_pct": 0.15,
}

SWEEPS: list[tuple[str, list[float | int]]] = [
    ("mr_sigma_entry", [2.0, 2.5, 3.0, 3.5, 4.0]),
    ("mr_min_net_edge_pct", [0.10, 0.20, 0.30, 0.40, 0.50]),
    ("mr_revalidation_min_spread_pct", [0.40, 0.50, 0.60, 0.70, 0.80]),
    ("mr_max_hold_seconds", [180, 300, 450, 600, 900]),
    ("mr_max_bbo_spread_bps", [5.0, 8.0, 10.0, 15.0]),
    ("mr_take_profit_fraction", [0.50, 0.60, 0.75, 0.85]),
    ("mr_sigma_stop", [4.0, 5.0, 6.0, 8.0]),
    ("mr_max_baseline_mean_pct", [0.30, 0.50, 0.70]),
]

RESULT_COLUMNS = [
    "run_id",
    "sweep_name",
    "param_name",
    "param_value",
    "mr_sigma_entry",
    "mr_min_net_edge_pct",
    "mr_revalidation_min_spread_pct",
    "mr_max_hold_seconds",
    "mr_max_bbo_spread_bps",
    "mr_take_profit_fraction",
    "mr_sigma_stop",
    "mr_min_stop_distance_pct",
    "mr_max_baseline_mean_pct",
    "trade_count",
    "total_net_pnl_usdt",
    "total_gross_pnl_usdt",
    "total_fees_usdt",
    "total_slippage_usdt",
    "winrate",
    "profit_factor",
    "sharpe_like",
    "max_drawdown_usdt",
    "mean_reversion_count",
    "mean_reversion_wr",
    "mean_reversion_avg_pnl",
    "timeout_count",
    "timeout_wr",
    "timeout_avg_pnl",
    "stop_loss_count",
    "stop_loss_wr",
    "stop_loss_avg_pnl",
    "stale_quote_count",
    "timeout_loss_count",
    "avg_hold_seconds",
    "score",
]


@dataclass(slots=True)
class BacktestRun:
    run_id: int
    sweep_name: str
    param_name: str
    param_value: str
    params: dict[str, float | int]
    trades: list[PaperTradeRecord]
    metrics: dict[str, float | int]


class InMemoryStore:
    def __init__(self) -> None:
        self.trades: list[PaperTradeRecord] = []

    def insert_paper_trade(self, record: PaperTradeRecord) -> int:
        self.trades.append(record)
        return len(self.trades)

    def update_opportunity_status(self, *_args: Any, **_kwargs: Any) -> None:
        return

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).isoformat()


def _parse_cli_datetime(value: str, *, end_of_day_exclusive: bool) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("Empty datetime argument")
    if "T" not in text:
        dt = datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=UTC)
        if end_of_day_exclusive:
            dt += timedelta(days=1)
        return dt
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@lru_cache(maxsize=500_000)
def _parse_snapshot_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@lru_cache(maxsize=500_000)
def _parse_tick_key(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _build_quote(
    *,
    exchange: ExchangeName,
    symbol: str,
    bid: float,
    ask: float,
    age_ms: float,
    timestamp: datetime,
) -> Quote | None:
    if bid <= 0 or ask <= 0:
        return None
    age = max(age_ms, 0.0)
    received_at = timestamp - timedelta(milliseconds=age) if age > 0 else timestamp
    return Quote(
        exchange=exchange,
        symbol=symbol,
        best_bid_price=Decimal(str(bid)),
        best_ask_price=Decimal(str(ask)),
        best_bid_size=Decimal("1000000"),
        best_ask_size=Decimal("1000000"),
        receive_latency_ms=0.0,
        source_latency_ms=age,
        received_at=received_at,
    )


def _iter_ticks(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
    until_iso: str,
    symbols: list[str],
):
    query = (
        "SELECT timestamp, symbol, exchange_a, exchange_b, bid_a, ask_a, bid_b, ask_b, quote_age_a_ms, quote_age_b_ms "
        "FROM spread_snapshots WHERE timestamp >= ? AND timestamp < ?"
    )
    params: list[Any] = [since_iso, until_iso]
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        query += f" AND symbol IN ({placeholders})"
        params.extend(symbols)
    query += " ORDER BY timestamp ASC"

    cursor = conn.execute(query, tuple(params))

    current_tick_key: str | None = None
    current_quotes: dict[tuple[ExchangeName, str], Quote] = {}
    for row in cursor:
        ts_text = str(row[0])
        tick_key = ts_text[:19]
        if current_tick_key is not None and tick_key != current_tick_key:
            yield _parse_tick_key(current_tick_key), current_quotes
            current_quotes = {}
        current_tick_key = tick_key

        symbol = str(row[1]).upper()
        try:
            exchange_a = ExchangeName(str(row[2]).lower())
            exchange_b = ExchangeName(str(row[3]).lower())
        except ValueError:
            continue

        ts = _parse_snapshot_ts(ts_text)
        quote_a = _build_quote(
            exchange=exchange_a,
            symbol=symbol,
            bid=float(row[4]),
            ask=float(row[5]),
            age_ms=float(row[8] or 0.0),
            timestamp=ts,
        )
        quote_b = _build_quote(
            exchange=exchange_b,
            symbol=symbol,
            bid=float(row[6]),
            ask=float(row[7]),
            age_ms=float(row[9] or 0.0),
            timestamp=ts,
        )
        if quote_a is not None:
            current_quotes[(exchange_a, symbol)] = quote_a
        if quote_b is not None:
            current_quotes[(exchange_b, symbol)] = quote_b

    if current_tick_key is not None:
        yield _parse_tick_key(current_tick_key), current_quotes


def _resolve_symbols(
    *,
    conn: sqlite3.Connection,
    symbols_arg: str,
    since_iso: str,
    until_iso: str,
    settings: Settings,
) -> list[str]:
    raw = symbols_arg.strip().lower()
    if raw == "env":
        return sorted({str(symbol).upper() for symbol in settings.symbols})
    if raw == "all":
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM spread_snapshots WHERE timestamp >= ? AND timestamp < ? ORDER BY symbol ASC",
            (since_iso, until_iso),
        ).fetchall()
        return [str(row[0]).upper() for row in rows]
    return [token.strip().upper() for token in symbols_arg.split(",") if token.strip()]


def _reason_stats(trades: list[PaperTradeRecord], reason: str) -> tuple[int, float, float]:
    subset = [trade for trade in trades if trade.close_reason == reason]
    count = len(subset)
    if count == 0:
        return (0, 0.0, 0.0)
    wins = sum(1 for trade in subset if trade.net_pnl_usdt > 0)
    total = sum(trade.net_pnl_usdt for trade in subset)
    return (count, wins / count, total / count)


def _max_drawdown_usdt(trades: list[PaperTradeRecord]) -> float:
    if not trades:
        return 0.0
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in trades:
        equity += trade.net_pnl_usdt
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return -max_drawdown


def _sharpe_like(trades: list[PaperTradeRecord]) -> float:
    n = len(trades)
    if n < 2:
        return 0.0
    values = [trade.net_pnl_usdt for trade in trades]
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    std = math.sqrt(max(variance, 0.0))
    if std <= 1e-12:
        return 0.0
    return mean / std * math.sqrt(n)


def _profit_factor(trades: list[PaperTradeRecord]) -> float:
    wins = sum(trade.net_pnl_usdt for trade in trades if trade.net_pnl_usdt > 0)
    losses = sum(trade.net_pnl_usdt for trade in trades if trade.net_pnl_usdt < 0)
    if abs(losses) <= 1e-12:
        return math.inf if wins > 0 else 0.0
    return wins / abs(losses)


def _score(metrics: dict[str, float | int]) -> float:
    trade_count = int(metrics["trade_count"])
    if trade_count < 5:
        return -1e9
    total_net = float(metrics["total_net_pnl_usdt"])
    winrate = float(metrics["winrate"])
    return total_net * (0.5 + 0.5 * winrate)


def _compute_metrics(trades: list[PaperTradeRecord]) -> dict[str, float | int]:
    trade_count = len(trades)
    total_net = sum(trade.net_pnl_usdt for trade in trades)
    total_gross = sum(trade.gross_pnl_usdt for trade in trades)
    total_fees = sum(trade.fees_usdt for trade in trades)
    total_slippage = sum(trade.slippage_usdt for trade in trades)
    wins = sum(1 for trade in trades if trade.net_pnl_usdt > 0)
    winrate = (wins / trade_count) if trade_count else 0.0
    mean_reversion_count, mean_reversion_wr, mean_reversion_avg_pnl = _reason_stats(trades, "mean_reversion")
    timeout_count, timeout_wr, timeout_avg_pnl = _reason_stats(trades, "timeout")
    stop_loss_count, stop_loss_wr, stop_loss_avg_pnl = _reason_stats(trades, "stop_loss")
    stale_quote_count = sum(1 for trade in trades if trade.close_reason == "stale_quote")
    timeout_loss_count = sum(1 for trade in trades if trade.close_reason == "timeout_loss")
    avg_hold_seconds = (sum(trade.hold_seconds for trade in trades) / trade_count) if trade_count else 0.0

    metrics: dict[str, float | int] = {
        "trade_count": trade_count,
        "total_net_pnl_usdt": total_net,
        "total_gross_pnl_usdt": total_gross,
        "total_fees_usdt": total_fees,
        "total_slippage_usdt": total_slippage,
        "winrate": winrate,
        "profit_factor": _profit_factor(trades),
        "sharpe_like": _sharpe_like(trades),
        "max_drawdown_usdt": _max_drawdown_usdt(trades),
        "mean_reversion_count": mean_reversion_count,
        "mean_reversion_wr": mean_reversion_wr,
        "mean_reversion_avg_pnl": mean_reversion_avg_pnl,
        "timeout_count": timeout_count,
        "timeout_wr": timeout_wr,
        "timeout_avg_pnl": timeout_avg_pnl,
        "stop_loss_count": stop_loss_count,
        "stop_loss_wr": stop_loss_wr,
        "stop_loss_avg_pnl": stop_loss_avg_pnl,
        "stale_quote_count": stale_quote_count,
        "timeout_loss_count": timeout_loss_count,
        "avg_hold_seconds": avg_hold_seconds,
    }
    metrics["score"] = _score(metrics)
    return metrics


def _result_row(result: BacktestRun) -> dict[str, Any]:
    row: dict[str, Any] = {
        "run_id": result.run_id,
        "sweep_name": result.sweep_name,
        "param_name": result.param_name,
        "param_value": result.param_value,
    }
    for key in [
        "mr_sigma_entry",
        "mr_min_net_edge_pct",
        "mr_revalidation_min_spread_pct",
        "mr_max_hold_seconds",
        "mr_max_bbo_spread_bps",
        "mr_take_profit_fraction",
        "mr_sigma_stop",
        "mr_min_stop_distance_pct",
        "mr_max_baseline_mean_pct",
    ]:
        row[key] = result.params[key]
    for key in RESULT_COLUMNS:
        if key in row:
            continue
        row[key] = result.metrics[key]
    return row


def _write_results(output_path: Path, runs: list[BacktestRun]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not output_path.exists() or output_path.stat().st_size == 0
    with output_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_COLUMNS)
        if write_header:
            writer.writeheader()
        for run in runs:
            writer.writerow(_result_row(run))


def _write_best_breakdowns(*, trades: list[PaperTradeRecord], output_path: Path) -> None:
    by_symbol: dict[str, list[PaperTradeRecord]] = {}
    by_pair: dict[str, list[PaperTradeRecord]] = {}
    by_hour: dict[int, list[PaperTradeRecord]] = {}

    for trade in trades:
        by_symbol.setdefault(trade.symbol, []).append(trade)
        pair = f"{trade.long_exchange}->{trade.short_exchange}"
        by_pair.setdefault(pair, []).append(trade)
        closed_at = _parse_snapshot_ts(trade.closed_at).astimezone(UTC)
        by_hour.setdefault(closed_at.hour, []).append(trade)

    def _rows(grouped: dict[Any, list[PaperTradeRecord]], label: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key, group in grouped.items():
            count = len(group)
            wins = sum(1 for trade in group if trade.net_pnl_usdt > 0)
            total = sum(trade.net_pnl_usdt for trade in group)
            rows.append(
                {
                    label: key,
                    "trade_count": count,
                    "winrate": (wins / count) if count else 0.0,
                    "total_net_pnl_usdt": total,
                    "avg_net_pnl_usdt": (total / count) if count else 0.0,
                }
            )
        rows.sort(key=lambda item: float(item["total_net_pnl_usdt"]), reverse=True)
        return rows

    symbol_rows = _rows(by_symbol, "symbol")
    pair_rows = _rows(by_pair, "pair")
    hour_rows = _rows(by_hour, "hour_utc")

    mapping = [
        ("backtest_best_by_symbol.csv", ["symbol", "trade_count", "winrate", "total_net_pnl_usdt", "avg_net_pnl_usdt"], symbol_rows),
        ("backtest_best_by_pair.csv", ["pair", "trade_count", "winrate", "total_net_pnl_usdt", "avg_net_pnl_usdt"], pair_rows),
        ("backtest_best_by_hour.csv", ["hour_utc", "trade_count", "winrate", "total_net_pnl_usdt", "avg_net_pnl_usdt"], hour_rows),
    ]
    for name, columns, rows in mapping:
        target = output_path.parent / name
        with target.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)


async def _run_single_backtest(
    *,
    conn: sqlite3.Connection,
    base_settings: Settings,
    run_id: int,
    sweep_name: str,
    param_name: str,
    param_value: str,
    params: dict[str, float | int],
    symbols: list[str],
    since_iso: str,
    until_iso: str,
) -> BacktestRun:
    overrides: dict[str, Any] = dict(params)
    overrides.update(
        {
            "symbols": symbols,
            "live_trading": False,
            "mr_revalidation_delay_sec": 0.0,
            "simulated_execution_delay_ms": 0,
        }
    )
    settings = base_settings.model_copy(update=overrides)
    store = InMemoryStore()
    state: dict[str, Any] = {
        "tick_ts": _parse_snapshot_ts(since_iso),
        "quotes": {},
    }

    def _clock() -> datetime:
        return state["tick_ts"]

    def _get_latest_quote(exchange: ExchangeName, symbol: str) -> Quote | None:
        return state["quotes"].get((exchange, symbol))

    engine = MeanReversionEngine(
        settings=settings,
        opportunity_store=store,  # type: ignore[arg-type]
        get_latest_quote=_get_latest_quote,
        execution_service=None,
        clock=_clock,
    )

    tick_count = 0
    for tick_ts, quotes in _iter_ticks(
        conn,
        since_iso=since_iso,
        until_iso=until_iso,
        symbols=symbols,
    ):
        tick_count += 1
        state["tick_ts"] = tick_ts
        state["quotes"] = quotes
        engine.update_baselines(quotes)
        engine.check_exits(quotes)
        await asyncio.sleep(0)

        pending_entries = [entry.task for entry in list(engine.pending_entries_by_symbol.values())]
        if pending_entries:
            await asyncio.gather(*pending_entries, return_exceptions=True)
        pending_closes = list(engine.pending_closes_by_symbol.values())
        if pending_closes:
            await asyncio.gather(*pending_closes, return_exceptions=True)
        await asyncio.sleep(0)

    pending_entries = [entry.task for entry in list(engine.pending_entries_by_symbol.values())]
    if pending_entries:
        await asyncio.gather(*pending_entries, return_exceptions=True)
    pending_closes = list(engine.pending_closes_by_symbol.values())
    if pending_closes:
        await asyncio.gather(*pending_closes, return_exceptions=True)

    metrics = _compute_metrics(store.trades)
    print(
        f"[run {run_id:02d}] sweep={sweep_name} {param_name}={param_value} "
        f"ticks={tick_count} trades={metrics['trade_count']} net={float(metrics['total_net_pnl_usdt']):+.2f} "
        f"wr={float(metrics['winrate']):.2f} score={float(metrics['score']):+.3f}"
    )
    return BacktestRun(
        run_id=run_id,
        sweep_name=sweep_name,
        param_name=param_name,
        param_value=param_value,
        params=dict(params),
        trades=list(store.trades),
        metrics=metrics,
    )


def _pick_best(runs: list[BacktestRun]) -> BacktestRun:
    return max(
        runs,
        key=lambda run: (
            float(run.metrics["score"]),
            float(run.metrics["total_net_pnl_usdt"]),
            float(run.metrics["winrate"]),
            int(run.metrics["trade_count"]),
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Historical backtester for MeanReversionEngine")
    parser.add_argument("--db", required=True, help="Path to SQLite DB")
    parser.add_argument("--since", required=True, help="Start date/datetime (inclusive, UTC if no tz)")
    parser.add_argument("--until", required=True, help="End date/datetime (exclusive; date means next day 00:00 UTC)")
    parser.add_argument("--symbols", default="env", help="'all', 'env', or comma-separated symbols")
    parser.add_argument("--output", default="backtest_results.csv", help="Output CSV path")
    return parser


async def _run() -> None:
    args = _build_parser().parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    since_dt = _parse_cli_datetime(args.since, end_of_day_exclusive=False)
    until_dt = _parse_cli_datetime(args.until, end_of_day_exclusive=True)
    if since_dt >= until_dt:
        raise SystemExit("--since must be earlier than --until")
    since_iso = since_dt.isoformat()
    until_iso = until_dt.isoformat()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        settings = Settings()
        symbols = _resolve_symbols(
            conn=conn,
            symbols_arg=args.symbols,
            since_iso=since_iso,
            until_iso=until_iso,
            settings=settings,
        )
        if not symbols:
            raise SystemExit("No symbols to backtest in the selected range")

        print(f"Backtest DB: {db_path}")
        print(f"Range: {since_iso} .. {until_iso} (exclusive)")
        print(f"Symbols: {len(symbols)}")

        all_runs: list[BacktestRun] = []
        run_id = 1
        current_params = dict(BASE_PARAMS)

        baseline = await _run_single_backtest(
            conn=conn,
            base_settings=settings,
            run_id=run_id,
            sweep_name="baseline",
            param_name="-",
            param_value="-",
            params=current_params,
            symbols=symbols,
            since_iso=since_iso,
            until_iso=until_iso,
        )
        all_runs.append(baseline)
        run_id += 1

        for sweep_name, values in SWEEPS:
            sweep_runs: list[BacktestRun] = []
            print(f"\nSweep: {sweep_name}")
            for value in values:
                params = dict(current_params)
                params[sweep_name] = value
                result = await _run_single_backtest(
                    conn=conn,
                    base_settings=settings,
                    run_id=run_id,
                    sweep_name=sweep_name,
                    param_name=sweep_name,
                    param_value=str(value),
                    params=params,
                    symbols=symbols,
                    since_iso=since_iso,
                    until_iso=until_iso,
                )
                sweep_runs.append(result)
                all_runs.append(result)
                run_id += 1
            best_for_sweep = _pick_best(sweep_runs)
            current_params[sweep_name] = best_for_sweep.params[sweep_name]
            print(
                f"Selected {sweep_name}={best_for_sweep.params[sweep_name]} "
                f"(score={float(best_for_sweep.metrics['score']):+.3f})"
            )

        output_path = Path(args.output)
        _write_results(output_path, all_runs)
        overall_best = _pick_best(all_runs)
        _write_best_breakdowns(trades=overall_best.trades, output_path=output_path)

        print("\nFinished")
        print(f"Runs: {len(all_runs)}")
        print(f"Results CSV: {output_path}")
        print(
            "Best run: "
            f"id={overall_best.run_id} score={float(overall_best.metrics['score']):+.3f} "
            f"net={float(overall_best.metrics['total_net_pnl_usdt']):+.2f} "
            f"trades={int(overall_best.metrics['trade_count'])}"
        )
    finally:
        conn.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
