#!/usr/bin/env python3
"""
Balance reconciliation: compare real exchange balances vs bot-calculated PnL.

Usage:
    python scripts/check_balances.py --db data/spread_arb.sqlite3
    python scripts/check_balances.py --db data/spread_arb.sqlite3 --since 2026-05-20
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from spread_arb.config import get_settings
from spread_arb.models import ExchangeName

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("check_balances")


LIVE_EXCHANGES = {
    ExchangeName.BINANCE: "binance",
    ExchangeName.OKX: "okx",
    ExchangeName.BYBIT: "bybit",
    ExchangeName.MEXC: "mexc",
}


def get_client_and_creds(settings: Any, exchange: ExchangeName, session: aiohttp.ClientSession) -> Any:
    """Create an authenticated exchange client."""
    if exchange == ExchangeName.BINANCE:
        from spread_arb.exchanges.binance import BinanceExchange
        return BinanceExchange(
            session=session,
            request_timeout_sec=settings.request_timeout_sec,
            api_key=settings.api_key_binance,
            api_secret=settings.api_secret_binance,
        )
    if exchange == ExchangeName.OKX:
        from spread_arb.exchanges.okx import OkxExchange
        return OkxExchange(
            session=session,
            request_timeout_sec=settings.request_timeout_sec,
            api_key=settings.api_key_okx,
            api_secret=settings.api_secret_okx,
            passphrase=settings.api_passphrase_okx,
        )
    if exchange == ExchangeName.BYBIT:
        from spread_arb.exchanges.bybit import BybitExchange
        return BybitExchange(
            session=session,
            request_timeout_sec=settings.request_timeout_sec,
            api_key=settings.api_key_bybit,
            api_secret=settings.api_secret_bybit,
        )
    if exchange == ExchangeName.MEXC:
        from spread_arb.exchanges.mexc import MexcExchange
        return MexcExchange(
            session=session,
            request_timeout_sec=settings.request_timeout_sec,
            api_key=settings.api_key_mexc,
            api_secret=settings.api_secret_mexc,
        )
    return None


def has_credentials(settings: Any, exchange: ExchangeName) -> bool:
    if exchange == ExchangeName.BINANCE:
        return bool(settings.api_key_binance.strip() and settings.api_secret_binance.strip())
    if exchange == ExchangeName.OKX:
        return bool(
            settings.api_key_okx.strip()
            and settings.api_secret_okx.strip()
            and settings.api_passphrase_okx.strip()
        )
    if exchange == ExchangeName.BYBIT:
        return bool(settings.api_key_bybit.strip() and settings.api_secret_bybit.strip())
    if exchange == ExchangeName.MEXC:
        return bool(settings.api_key_mexc.strip() and settings.api_secret_mexc.strip())
    return False


def get_bot_pnl(db_path: str, since: str | None = None) -> dict[str, float | int]:
    """Get bot-calculated PnL from closed paper trades."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = """
                SELECT
                    COUNT(*) AS trade_count,
                    COALESCE(SUM(net_pnl_usdt), 0.0) AS total_pnl
                FROM paper_trades
                WHERE close_reason IS NOT NULL
            """
            params: list[str] = []
            if since:
                query += " AND opened_at >= ?"
                params.append(since)
            row = conn.execute(query, params).fetchone()
    except sqlite3.OperationalError:
        return {"total_pnl": 0.0, "trade_count": 0}

    return {
        "total_pnl": float(row["total_pnl"]) if row is not None else 0.0,
        "trade_count": int(row["trade_count"]) if row is not None else 0,
    }


def get_baseline_snapshots(
    db_path: str,
    exchanges: list[str],
    since: str | None = None,
) -> tuple[dict[str, float], str | None]:
    """Return earliest startup snapshot per exchange plus period start timestamp."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = """
                SELECT exchange, total_usdt, timestamp
                FROM balance_snapshots
                WHERE snapshot_type = 'startup'
            """
            params: list[str] = []
            if since:
                query += " AND timestamp >= ?"
                params.append(since)
            query += " ORDER BY timestamp ASC, id ASC"
            rows = conn.execute(query, params).fetchall()
    except sqlite3.OperationalError:
        return {}, None

    baseline: dict[str, float] = {}
    first_ts: str | None = None
    for row in rows:
        exchange = str(row["exchange"])
        if exchange not in exchanges or exchange in baseline:
            continue
        baseline[exchange] = float(row["total_usdt"])
        if first_ts is None or str(row["timestamp"]) < first_ts:
            first_ts = str(row["timestamp"])
    return baseline, first_ts


async def run_reconciliation(db_path: str, since: str | None = None) -> int:
    settings = get_settings()
    active_exchanges = [
        exchange
        for exchange in settings.exchanges
        if exchange in LIVE_EXCHANGES and has_credentials(settings, exchange)
    ]

    if not active_exchanges:
        log.error("No supported exchanges with credentials configured in .env")
        return 1

    print("=" * 64)
    print("BALANCE RECONCILIATION REPORT")
    print("=" * 64)

    current_balances: dict[str, float] = {}
    async with aiohttp.ClientSession() as session:
        for exchange in active_exchanges:
            try:
                client = get_client_and_creds(settings, exchange, session)
                if client is None:
                    continue
                balance = await client.get_balance()
                current_balances[exchange.value] = float(balance.total_usdt)
                log.info("fetched balance for %s: %.2f USDT", exchange.value, balance.total_usdt)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to fetch balance for %s: %s", exchange.value, exc)

    if not current_balances:
        log.error("Failed to fetch balances from all active exchanges")
        return 1

    start_balances, first_snapshot_ts = get_baseline_snapshots(
        db_path=db_path,
        exchanges=list(current_balances.keys()),
        since=since,
    )
    bot_data = get_bot_pnl(db_path, since)

    period_start = since or first_snapshot_ts or "unknown"
    period_end = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    print(f"Period: {period_start} - {period_end}")

    print("\nEXCHANGE BALANCES")
    print("-" * 40)

    total_start = 0.0
    total_current = 0.0
    for exchange_name in sorted(current_balances.keys()):
        current = current_balances[exchange_name]
        start = start_balances.get(exchange_name, 0.0)
        change = current - start
        total_current += current
        total_start += start
        if exchange_name in start_balances:
            print(
                f"  {exchange_name.capitalize():10s}  "
                f"start=${start:.2f}  current=${current:.2f}  change=${change:+.2f}"
            )
        else:
            print(
                f"  {exchange_name.capitalize():10s}  "
                f"start=N/A     current=${current:.2f}  (no startup snapshot baseline)"
            )

    total_change = total_current - total_start
    print(
        f"\n  {'Total':10s}  "
        f"start=${total_start:.2f}  current=${total_current:.2f}  change=${total_change:+.2f}"
    )

    print("\nBOT-CALCULATED PNL")
    print("-" * 40)
    print(f"  Trades: {bot_data['trade_count']}")
    print(f"  Sum of net_pnl_usdt from trades: ${bot_data['total_pnl']:+.2f}")

    print("\nRECONCILIATION")
    print("-" * 40)
    print(f"  Real balance change:  ${total_change:+.2f}")
    print(f"  Bot calculated PnL:   ${bot_data['total_pnl']:+.2f}")
    discrepancy = total_change - float(bot_data["total_pnl"])
    if bot_data["total_pnl"] != 0:
        discrepancy_pct = abs(discrepancy / float(bot_data["total_pnl"])) * 100.0
    else:
        discrepancy_pct = 0.0
    print(f"  Discrepancy:          ${discrepancy:+.2f} ({discrepancy_pct:.1f}%)")

    if abs(discrepancy) > 0.5:
        print("\n  Possible causes of discrepancy:")
        print("  - Funding rate payments (not tracked by bot)")
        print("  - Rounding differences in fee calculation")
        print("  - Partial fills with different qty than expected")
        print("  - Manual trades on exchange")
    else:
        print("\n  Balances match within tolerance")

    print("=" * 64)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Balance reconciliation report")
    parser.add_argument("--db", required=True, help="Path to SQLite database")
    parser.add_argument("--since", default=None, help="Start date/time filter (e.g. 2026-05-20)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    exit_code = asyncio.run(run_reconciliation(str(db_path), args.since))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
