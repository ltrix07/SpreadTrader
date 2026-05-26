#!/usr/bin/env python3
"""Standalone diagnostic для backtester.

Запускает один прогон с очень мягкими настройками и DEBUG-логом, печатает
сколько baseline'ов стали ready, какие фильтры режут сигналы.

Usage:
    python scripts/backtest_diagnose.py --db src/data/spread_arb_proj.sqlite3 \
        --since 2026-05-20 --until 2026-05-25 --symbols env
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from spread_arb.config import Settings
from spread_arb.mean_reversion_engine import MeanReversionEngine
from spread_arb.models import ExchangeName, Quote
from spread_arb.storage import PaperTradeRecord


# ВСЕ ФИЛЬТРЫ МАКСИМАЛЬНО МЯГКИЕ — должны давать кучу сигналов
RELAXED_PARAMS: dict[str, Any] = {
    "mr_sigma_entry": 1.5,
    "mr_min_net_edge_pct": 0.0,
    "mr_revalidation_min_spread_pct": 0.05,
    "mr_max_hold_seconds": 900,
    "mr_max_bbo_spread_bps": 50.0,
    "mr_max_baseline_mean_pct": 5.0,
    "mr_take_profit_fraction": 0.50,
    "mr_sigma_stop": 8.0,
    "mr_min_stop_distance_pct": 0.30,
    "mr_min_top_capacity_multiplier": 1.0,
    "mr_min_quote_freshness_pct": 0.0,
    "mr_cooldown_sec": 0,
}


class InMemoryStore:
    def __init__(self) -> None:
        self.trades: list[PaperTradeRecord] = []

    def insert_paper_trade(self, record: PaperTradeRecord) -> int:
        self.trades.append(record)
        return len(self.trades)

    def update_opportunity_status(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).isoformat()


def _parse_cli_datetime(value: str, *, end_of_day_exclusive: bool) -> datetime:
    text = value.strip()
    if "T" not in text:
        dt = datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=UTC)
        if end_of_day_exclusive:
            dt += timedelta(days=1)
        return dt
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_snapshot_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _build_quote(*, exchange: ExchangeName, symbol: str, bid: float, ask: float, age_ms: float, ts: datetime) -> Quote | None:
    if bid <= 0 or ask <= 0:
        return None
    age = max(age_ms, 0.0)
    received_at = ts - timedelta(milliseconds=age) if age > 0 else ts
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


def _iter_ticks(conn: sqlite3.Connection, *, since_iso: str, until_iso: str, symbols: list[str]):
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

    current_key: str | None = None
    current_quotes: dict[tuple[ExchangeName, str], Quote] = {}
    for row in cursor:
        ts_text = str(row[0])
        tick_key = ts_text[:19]
        if current_key is not None and tick_key != current_key:
            yield _parse_snapshot_ts(current_key + "+00:00" if "+" not in current_key else current_key), current_quotes
            current_quotes = {}
        current_key = tick_key

        symbol = str(row[1]).upper()
        try:
            exchange_a = ExchangeName(str(row[2]).lower())
            exchange_b = ExchangeName(str(row[3]).lower())
        except ValueError:
            continue
        ts = _parse_snapshot_ts(ts_text)
        q_a = _build_quote(exchange=exchange_a, symbol=symbol, bid=float(row[4]), ask=float(row[5]), age_ms=float(row[8] or 0.0), ts=ts)
        q_b = _build_quote(exchange=exchange_b, symbol=symbol, bid=float(row[6]), ask=float(row[7]), age_ms=float(row[9] or 0.0), ts=ts)
        if q_a is not None:
            current_quotes[(exchange_a, symbol)] = q_a
        if q_b is not None:
            current_quotes[(exchange_b, symbol)] = q_b

    if current_key is not None:
        yield _parse_snapshot_ts(current_key + "+00:00" if "+" not in current_key else current_key), current_quotes


async def _run(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    since_dt = _parse_cli_datetime(args.since, end_of_day_exclusive=False)
    until_dt = _parse_cli_datetime(args.until, end_of_day_exclusive=True)
    since_iso = since_dt.isoformat()
    until_iso = until_dt.isoformat()

    conn = sqlite3.connect(db_path)
    try:
        base_settings = Settings()
        if args.symbols == "env":
            symbols = sorted({str(s).upper() for s in base_settings.symbols})
        elif args.symbols == "all":
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM spread_snapshots WHERE timestamp >= ? AND timestamp < ? ORDER BY symbol",
                (since_iso, until_iso),
            ).fetchall()
            symbols = [str(r[0]).upper() for r in rows]
        else:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

        overrides: dict[str, Any] = dict(RELAXED_PARAMS)
        overrides.update({
            "symbols": symbols,
            "live_trading": False,
            "mr_revalidation_delay_sec": 0.0,
            "simulated_execution_delay_ms": 0,
            "mr_rolling_window": args.rolling_window,
        })
        settings = base_settings.model_copy(update=overrides)

        store = InMemoryStore()
        state: dict[str, Any] = {"tick_ts": since_dt, "quotes": {}}

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

        print(f"Diagnostic settings:")
        for k, v in overrides.items():
            if k == "symbols":
                print(f"  {k}: {len(v)} items")
            else:
                print(f"  {k}: {v}")
        print()

        tick_count = 0
        last_print_tick = 0
        for tick_ts, quotes in _iter_ticks(conn, since_iso=since_iso, until_iso=until_iso, symbols=symbols):
            tick_count += 1
            state["tick_ts"] = tick_ts
            state["quotes"] = quotes
            engine.update_baselines(quotes)
            engine.check_exits(quotes)
            await asyncio.sleep(0)
            pending = [p.task for p in list(engine.pending_entries_by_symbol.values())]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            closes = list(engine.pending_closes_by_symbol.values())
            if closes:
                await asyncio.gather(*closes, return_exceptions=True)

            if tick_count - last_print_tick >= 1000:
                last_print_tick = tick_count
                print(
                    f"  tick={tick_count} ts={tick_ts.isoformat()[:19]} "
                    f"baselines={len(engine.baselines)} ready={len(engine.baseline_ready_keys)} "
                    f"trades={len(store.trades)}"
                )

        # Final summary
        print()
        print(f"=== FINAL ===")
        print(f"Total ticks:         {tick_count}")
        print(f"Baselines created:   {len(engine.baselines)}")
        print(f"Baselines ready:     {len(engine.baseline_ready_keys)}")
        print(f"Trades opened:       {len(store.trades)}")
        if engine.close_reason_counts:
            print(f"Close reasons:       {dict(engine.close_reason_counts)}")

        # Show top 10 baselines by sample count
        top_baselines = sorted(
            engine.baselines.items(),
            key=lambda kv: len(kv[1].window),
            reverse=True,
        )[:10]
        print()
        print("Top 10 baselines by sample count:")
        for key, bl in top_baselines:
            sym, ex_a, ex_b = key
            ready = "READY" if bl.is_ready else "warm "
            print(f"  [{ready}] {sym:<14} {ex_a.value}->{ex_b.value:<8} samples={len(bl.window):<4} mean={bl.mean:+.4f}% std={bl.std:.4f}%")

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnostic backtest run")
    parser.add_argument("--db", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--symbols", default="env")
    parser.add_argument("--debug", action="store_true", help="Show DEBUG logs (very verbose)")
    parser.add_argument("--rolling-window", type=int, default=60, help="Smaller window for faster warmup (default 60 = 10min)")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
