# Task: Add Balance Tracking & Daily Reconciliation

## Overview

Add a balance tracking system that periodically snapshots real exchange balances and provides a reconciliation script to compare bot-calculated PnL vs actual balance changes. This is a safety net to catch any discrepancies (missed fills, unaccounted fees, funding rates, etc.).

## Step 1: Add `balance_snapshots` table to `src/spread_arb/storage.py`

Add a new table for storing balance history:

```sql
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    exchange TEXT NOT NULL,
    total_usdt REAL NOT NULL,
    available_usdt REAL NOT NULL,
    snapshot_type TEXT NOT NULL DEFAULT 'periodic'  -- 'periodic', 'startup', 'manual'
)
```

Add an index on `(timestamp, exchange)`.

Add two methods to the storage module:

```python
async def save_balance_snapshot(
    self,
    exchange: str,
    total_usdt: float,
    available_usdt: float,
    snapshot_type: str = "periodic",
) -> None:
    """Insert a balance snapshot row."""
    ...

async def get_balance_snapshots(
    self,
    since: str | None = None,
    exchange: str | None = None,
) -> list[dict]:
    """Retrieve balance snapshots, optionally filtered by time and exchange."""
    ...
```

## Step 2: Add balance snapshot task to `src/spread_arb/scanner.py`

When `live_trading=True`, add a periodic task that runs every 30 minutes (configurable via `BALANCE_SNAPSHOT_INTERVAL_SEC=1800` in config):

```python
async def _snapshot_balances(self) -> None:
    """Periodically snapshot balances on all active exchanges."""
    while True:
        await asyncio.sleep(self.settings.balance_snapshot_interval_sec)
        for exchange_name, client in self.live_clients.items():
            try:
                balance = await client.get_balance()
                await self.opportunity_store.save_balance_snapshot(
                    exchange=exchange_name.value,
                    total_usdt=float(balance.total_usdt),
                    available_usdt=float(balance.available_usdt),
                    snapshot_type="periodic",
                )
            except Exception as exc:
                self.log.warning("balance snapshot failed for %s: %s", exchange_name.value, exc)
```

Also take a snapshot at startup (snapshot_type="startup") right after `ExecutionService.initialize()` completes.

Add to `config.py`:
```python
balance_snapshot_interval_sec: int = Field(default=1800, ge=60)
```

## Step 3: Create `scripts/check_balances.py`

A standalone script that:

1. Connects to all configured exchanges (reads API keys from `.env`)
2. Fetches current balance on each exchange
3. Reads the earliest "startup" snapshot from the database as the baseline
4. Calculates real PnL per exchange: `current_balance - first_snapshot_balance`
5. Reads bot-calculated PnL from `paper_trades` table (sum of net_pnl_usdt, grouped by exchange)
6. Compares and shows discrepancy

Output format:
```
================================================================
BALANCE RECONCILIATION REPORT
================================================================
Period: 2026-05-20 08:00 - 2026-05-20 20:00

EXCHANGE BALANCES
----------------------------------------
  Binance:  start=$12.50  current=$13.82  change=+$1.32
  OKX:      start=$12.50  current=$11.95  change=-$0.55
  Bybit:    start=$12.50  current=$13.10  change=+$0.60
  MEXC:     start=$12.50  current=$12.78  change=+$0.28
  
  Total:    start=$50.00  current=$51.65  change=+$1.65

BOT-CALCULATED PNL
----------------------------------------
  Sum of net_pnl_usdt from trades: +$1.58

RECONCILIATION
----------------------------------------
  Real balance change:  +$1.65
  Bot calculated PnL:   +$1.58
  Discrepancy:          +$0.07 (4.2%)
  
  Possible causes of discrepancy:
  - Funding rate payments (not tracked by bot)
  - Rounding differences in fee calculation
  - Partial fills with different qty than expected
================================================================
```

Usage:
```bash
python scripts/check_balances.py --db data/spread_arb.sqlite3
python scripts/check_balances.py --db data/spread_arb.sqlite3 --since 2026-05-20
```

### Implementation:

```python
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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiohttp
from spread_arb.config import get_settings
from spread_arb.models import ExchangeName

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("check_balances")

# Map exchange names to their client classes and credential extractors
LIVE_EXCHANGES = {
    ExchangeName.BINANCE: "binance",
    ExchangeName.OKX: "okx",
    ExchangeName.BYBIT: "bybit",
    ExchangeName.MEXC: "mexc",
}


def get_client_and_creds(settings, exchange: ExchangeName, session):
    """Create an authenticated exchange client."""
    if exchange == ExchangeName.BINANCE:
        from spread_arb.exchanges.binance import BinanceExchange
        return BinanceExchange(
            session=session,
            request_timeout_sec=settings.request_timeout_sec,
            api_key=settings.api_key_binance,
            api_secret=settings.api_secret_binance,
        )
    elif exchange == ExchangeName.OKX:
        from spread_arb.exchanges.okx import OkxExchange
        return OkxExchange(
            session=session,
            request_timeout_sec=settings.request_timeout_sec,
            api_key=settings.api_key_okx,
            api_secret=settings.api_secret_okx,
            passphrase=settings.api_passphrase_okx,
        )
    elif exchange == ExchangeName.BYBIT:
        from spread_arb.exchanges.bybit import BybitExchange
        return BybitExchange(
            session=session,
            request_timeout_sec=settings.request_timeout_sec,
            api_key=settings.api_key_bybit,
            api_secret=settings.api_secret_bybit,
        )
    elif exchange == ExchangeName.MEXC:
        from spread_arb.exchanges.mexc import MexcExchange
        return MexcExchange(
            session=session,
            request_timeout_sec=settings.request_timeout_sec,
            api_key=settings.api_key_mexc,
            api_secret=settings.api_secret_mexc,
        )
    return None


def get_bot_pnl(db_path: str, since: str | None = None) -> dict:
    """Get bot-calculated PnL from paper_trades, grouped by exchange pair."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    query = "SELECT * FROM paper_trades WHERE close_reason IS NOT NULL"
    params = []
    if since:
        query += " AND opened_at >= ?"
        params.append(since)
    
    rows = conn.execute(query, params).fetchall()
    conn.close()
    
    # Aggregate PnL by exchange (each trade touches two exchanges)
    # We can't split PnL per exchange from trade data alone,
    # so just return total
    total_pnl = sum(r["net_pnl_usdt"] for r in rows)
    trade_count = len(rows)
    
    return {"total_pnl": total_pnl, "trade_count": trade_count}


def get_first_snapshot(db_path: str, since: str | None = None) -> dict[str, float]:
    """Get the earliest balance snapshot per exchange."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    query = """
        SELECT exchange, total_usdt, timestamp
        FROM balance_snapshots
        WHERE 1=1
    """
    params = []
    if since:
        query += " AND timestamp >= ?"
        params.append(since)
    
    query += " ORDER BY timestamp ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    
    # First snapshot per exchange
    result = {}
    for row in rows:
        ex = row["exchange"]
        if ex not in result:
            result[ex] = float(row["total_usdt"])
    return result


async def run_reconciliation(db_path: str, since: str | None = None):
    settings = get_settings()
    
    # Determine which exchanges have API keys configured
    active_exchanges = []
    for ex in settings.exchanges:
        if ex in LIVE_EXCHANGES:
            # Check if credentials exist
            key_attr = f"api_key_{LIVE_EXCHANGES[ex]}"
            if getattr(settings, key_attr, ""):
                active_exchanges.append(ex)
    
    if not active_exchanges:
        log.error("No exchanges with API keys configured. Fill in API keys in .env")
        return
    
    # Fetch current balances
    print("=" * 60)
    print("BALANCE RECONCILIATION REPORT")
    print("=" * 60)
    
    current_balances = {}
    async with aiohttp.ClientSession() as session:
        for ex in active_exchanges:
            try:
                client = get_client_and_creds(settings, ex, session)
                if client:
                    balance = await client.get_balance()
                    current_balances[ex.value] = float(balance.total_usdt)
                    log.info("Fetched balance for %s: %.2f USDT", ex.value, balance.total_usdt)
            except Exception as exc:
                log.warning("Failed to fetch balance for %s: %s", ex.value, exc)
    
    # Get starting balances from snapshots
    start_balances = get_first_snapshot(db_path, since)
    
    # Get bot PnL
    bot_data = get_bot_pnl(db_path, since)
    
    # Print report
    period_start = since or "first snapshot"
    period_end = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    print(f"\nPeriod: {period_start} - {period_end}")
    
    print("\nEXCHANGE BALANCES")
    print("-" * 40)
    
    total_start = 0.0
    total_current = 0.0
    
    for ex_name, current in sorted(current_balances.items()):
        start = start_balances.get(ex_name, 0.0)
        change = current - start
        total_start += start
        total_current += current
        
        if start > 0:
            print(f"  {ex_name:10s}  start=${start:.2f}  current=${current:.2f}  change=${change:+.2f}")
        else:
            print(f"  {ex_name:10s}  start=N/A     current=${current:.2f}  (no baseline snapshot)")
    
    total_change = total_current - total_start
    print(f"\n  {'Total':10s}  start=${total_start:.2f}  current=${total_current:.2f}  change=${total_change:+.2f}")
    
    print(f"\nBOT-CALCULATED PNL")
    print("-" * 40)
    print(f"  Trades: {bot_data['trade_count']}")
    print(f"  Sum of net_pnl_usdt: ${bot_data['total_pnl']:+.2f}")
    
    print(f"\nRECONCILIATION")
    print("-" * 40)
    print(f"  Real balance change:  ${total_change:+.2f}")
    print(f"  Bot calculated PnL:   ${bot_data['total_pnl']:+.2f}")
    discrepancy = total_change - bot_data["total_pnl"]
    if bot_data["total_pnl"] != 0:
        disc_pct = abs(discrepancy / bot_data["total_pnl"]) * 100
    else:
        disc_pct = 0.0
    print(f"  Discrepancy:          ${discrepancy:+.2f} ({disc_pct:.1f}%)")
    
    if abs(discrepancy) > 0.5:
        print(f"\n  ⚠ Possible causes:")
        print(f"  - Funding rate payments (not tracked by bot)")
        print(f"  - Rounding in fee calculation")
        print(f"  - Partial fills with different qty")
        print(f"  - Manual trades on the exchange")
    else:
        print(f"\n  ✓ Balances match within tolerance")
    
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Balance reconciliation report")
    parser.add_argument("--db", required=True, help="Path to SQLite database")
    parser.add_argument("--since", default=None, help="Start date (YYYY-MM-DD)")
    args = parser.parse_args()
    
    asyncio.run(run_reconciliation(args.db, args.since))


if __name__ == "__main__":
    main()
```

## Step 4: Add startup balance check to `src/spread_arb/scanner.py`

When `live_trading=True`, at startup after initializing the execution service, log the balance on each exchange and save a startup snapshot:

```python
# In run() method, after execution_service.initialize():
if self.settings.live_trading:
    for exchange_name, client in live_clients.items():
        try:
            balance = await client.get_balance()
            self.log.info(
                "startup balance | %s | total=%.2f available=%.2f",
                exchange_name.value, balance.total_usdt, balance.available_usdt,
            )
            await self.opportunity_store.save_balance_snapshot(
                exchange=exchange_name.value,
                total_usdt=float(balance.total_usdt),
                available_usdt=float(balance.available_usdt),
                snapshot_type="startup",
            )
        except Exception as exc:
            self.log.warning("failed to get startup balance for %s: %s", exchange_name.value, exc)
```

Also start the periodic snapshot task:
```python
if self.settings.live_trading:
    asyncio.create_task(self._snapshot_balances())
```

## Files to change

| File | Action | Description |
|------|--------|-------------|
| `src/spread_arb/config.py` | MODIFY | Add `balance_snapshot_interval_sec` field |
| `src/spread_arb/storage.py` | MODIFY | Add `balance_snapshots` table creation, `save_balance_snapshot()`, `get_balance_snapshots()` |
| `src/spread_arb/scanner.py` | MODIFY | Add startup balance logging/snapshot, periodic `_snapshot_balances()` task |
| `scripts/check_balances.py` | CREATE | Reconciliation report script |

## Verification

1. `python -c "from spread_arb.storage import OpportunityStore"` — no import errors
2. Run bot with `LIVE_TRADING=true` — check logs for "startup balance" messages
3. Run `python scripts/check_balances.py --db data/spread_arb.sqlite3` — should show report
4. After 30 min, check that `balance_snapshots` table has periodic entries
