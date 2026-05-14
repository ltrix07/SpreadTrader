# Task: Fix stale_quote exit problem + create paper trading statistics script

## Part 1: Fix stale_quote forced exits in MeanReversionEngine

### Problem

The `MeanReversionEngine.check_exits()` in `src/spread_arb/mean_reversion_engine.py` uses `self.settings.max_quote_age_ms` (default 2000ms) to decide if an open position should be force-closed due to stale quotes. This is the same threshold used for ENTRY freshness.

Result: the bot enters a position when a fresh quote arrives, but within 1-2 seconds the quote age exceeds 2000ms and the position is immediately force-closed with `close_reason="stale_quote"`. The spread never gets a chance to revert. Out of 10 trades observed, 8 were closed as `stale_quote` with losses (full roundtrip fees paid for 1-3 second holds).

Worst case: ZENUSDT with HTX (avg quote age 3700ms) — bot entered and was force-closed twice in a row at -$8.26 and -$8.10 per trade because HTX quotes are inherently slower.

### Evidence from logs

```
mr open  | ZENUSDT  | long=okx @ 6.597 | short=htx @ 6.604 | spread=+0.11%
mr close | ZENUSDT  | reason=stale_quote | hold=1s | net_pnl=-8.26 USDT
mr open  | ZENUSDT  | long=okx @ 6.591 | short=htx @ 6.605 | spread=+0.21%  
mr close | ZENUSDT  | reason=stale_quote | hold=1s | net_pnl=-8.10 USDT

mr open  | OPUSDT   | long=okx @ 0.1443 | short=htx @ 0.1453 | spread=+0.69%
mr close | OPUSDT   | reason=stale_quote | hold=2s | net_pnl=-1.77 USDT

mr open  | AVAXUSDT | long=bitget @ 9.823 | short=binance @ 9.867 | spread=+0.45%
mr close | AVAXUSDT | reason=mean_reversion | hold=3s | net_pnl=+0.45 USDT  ← this one worked!
```

### Fix required

**1. Add a separate exit quote age threshold to config (`src/spread_arb/config.py`):**

```python
mr_exit_max_quote_age_ms: int = Field(default=10_000, ge=1)  # 10 seconds for exit monitoring
```

The entry threshold (`max_quote_age_ms=2000`) stays strict — we need fresh prices to enter. But once in a position, we can tolerate older quotes for monitoring and only force-close when quotes are REALLY stale (10+ seconds).

**2. Add per-exchange entry freshness override:**

Some exchanges (HTX specifically, avg quote age 3700ms) are inherently slow. The 2000ms entry threshold means we CAN enter with their quotes when they happen to be fresh, but those quotes go stale almost immediately. Solution: add a configurable set of exchanges to exclude from MR entry signals.

```python
mr_excluded_exchanges: list[str] = Field(default_factory=lambda: ["htx"])
```

In `_evaluate_signal()`, skip if either `long_exchange` or `short_exchange` is in this excluded list.

Note: HTX should still participate in baseline calculation and spread snapshots — we just don't TRADE on HTX pairs in MR mode.

**3. Modify `check_exits()` in `src/spread_arb/mean_reversion_engine.py`:**

Current code (lines 266-269):
```python
elif age_long_ms > max_age_ms or age_short_ms > max_age_ms:
    close_reason = "stale_quote"
```

Change to use the new exit threshold:
```python
exit_max_age_ms = self.settings.mr_exit_max_quote_age_ms
# ...
elif age_long_ms > exit_max_age_ms or age_short_ms > exit_max_age_ms:
    close_reason = "stale_quote"
```

**4. Modify `_evaluate_signal()` to check excluded exchanges:**

Add near the top of `_evaluate_signal()`:
```python
if long_exchange.value in self.settings.mr_excluded_exchanges:
    return
if short_exchange.value in self.settings.mr_excluded_exchanges:
    return
```

**5. Add stale_quote grace behavior:**

When quotes are stale but not yet at exit threshold, the bot should NOT update `max_adverse_spread_pct` / `max_favorable_spread_pct` since the spread calculation from stale quotes is unreliable. Only update these tracking fields when both quotes are reasonably fresh (e.g., < 5 seconds).

### Files to modify

- `src/spread_arb/config.py` — add `mr_exit_max_quote_age_ms` and `mr_excluded_exchanges`
- `src/spread_arb/mean_reversion_engine.py` — use new thresholds in `check_exits()` and `_evaluate_signal()`

---

## Part 2: Create paper trading statistics script

### Purpose

Create `scripts/analyze_paper_trades.py` that reads the `paper_trades` table and outputs a clear, comprehensive report of MR paper trading performance.

### Database schema for reference

```sql
CREATE TABLE paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    long_exchange TEXT NOT NULL,
    short_exchange TEXT NOT NULL,
    notional_usdt REAL NOT NULL,
    opened_at TEXT NOT NULL,        -- ISO timestamp
    closed_at TEXT NOT NULL,         -- ISO timestamp
    hold_seconds REAL NOT NULL,
    entry_long_price REAL NOT NULL,
    entry_short_price REAL NOT NULL,
    exit_long_price REAL NOT NULL,
    exit_short_price REAL NOT NULL,
    entry_raw_spread_pct REAL NOT NULL,
    exit_raw_spread_pct REAL NOT NULL,
    gross_pnl_usdt REAL NOT NULL,
    fees_usdt REAL NOT NULL,
    slippage_usdt REAL NOT NULL,
    funding_usdt REAL NOT NULL,
    net_pnl_usdt REAL NOT NULL,
    net_pnl_pct REAL NOT NULL,
    close_reason TEXT NOT NULL,      -- mean_reversion, stop_loss, timeout, stale_quote
    max_adverse_spread_pct REAL NOT NULL,
    max_favorable_spread_pct REAL NOT NULL,
    created_at TEXT NOT NULL
);
```

### Output format

The script should print a report like this:

```
================================================================
MEAN REVERSION PAPER TRADING REPORT
================================================================
Period: 2026-05-14 10:30 — 2026-05-14 22:30 (12.0 hours)
Total trades: 47
Starting notional: $350/leg ($700 total exposure)

OVERALL PERFORMANCE
────────────────────────────────────────
  Net PnL:           +$12.84
  Gross PnL:         +$28.50
  Total fees:        -$11.20
  Total slippage:    -$4.46
  Return on capital: +1.83% (on $700)
  Annualized:        +668%

WIN/LOSS BREAKDOWN
────────────────────────────────────────
  Wins:    31 (65.9%)    avg: +$1.24    total: +$38.44
  Losses:  16 (34.1%)    avg: -$1.60    total: -$25.60
  Profit factor: 1.50 (gross wins / gross losses)
  Avg win / avg loss ratio: 0.78

CLOSE REASONS
────────────────────────────────────────
  mean_reversion:  28 (59.6%)  avg_pnl: +$1.45  avg_hold: 45s
  timeout:          8 (17.0%)  avg_pnl: -$0.32  avg_hold: 900s
  stale_quote:      7 (14.9%)  avg_pnl: -$1.80  avg_hold: 3s
  stop_loss:        4  (8.5%)  avg_pnl: -$3.20  avg_hold: 120s

HOLD TIME DISTRIBUTION
────────────────────────────────────────
  < 10s:     12 trades   avg_pnl: +$0.15
  10s-60s:   18 trades   avg_pnl: +$0.85
  1m-5m:     10 trades   avg_pnl: +$0.42
  5m-15m:     7 trades   avg_pnl: -$0.90
  > 15m:      0 trades

TOP SYMBOLS BY NET PNL
────────────────────────────────────────
  INJUSDT:    8 trades  net: +$6.20  avg: +$0.78  winrate: 75.0%
  IMXUSDT:    6 trades  net: +$4.10  avg: +$0.68  winrate: 66.7%
  CFXUSDT:    5 trades  net: +$2.80  avg: +$0.56  winrate: 80.0%
  DOGEUSDT:   4 trades  net: -$0.26  avg: -$0.07  winrate: 50.0%
  ...

TOP EXCHANGE PAIRS BY NET PNL
────────────────────────────────────────
  bitget->okx:   12 trades  net: +$8.40  avg: +$0.70
  bybit->okx:    10 trades  net: +$5.20  avg: +$0.52
  okx->bybit:     8 trades  net: +$1.50  avg: +$0.19
  okx->htx:       5 trades  net: -$4.80  avg: -$0.96  ← problematic
  ...

WORST TRADES
────────────────────────────────────────
  #1: ZENUSDT okx->htx | -$8.26 | hold=1s | reason=stale_quote
  #2: ZENUSDT okx->htx | -$8.10 | hold=1s | reason=stale_quote
  #3: OPUSDT okx->htx  | -$1.77 | hold=2s | reason=stale_quote
  ...

EQUITY CURVE (hourly)
────────────────────────────────────────
  10:00  $0.00
  11:00  +$1.20
  12:00  +$3.45  ▲
  13:00  +$2.80  ▼
  14:00  +$5.10  ▲
  ...
```

### CLI arguments

```
python scripts/analyze_paper_trades.py --db data/spread_arb.sqlite3 [--since 2026-05-14] [--notional 350]
```

- `--db`: path to SQLite database (default: data/spread_arb.sqlite3)
- `--since`: only include trades opened after this date/datetime (ISO format). If omitted, include all trades.
- `--notional`: notional per leg for return calculation (default: 350)
- `--mr-only`: if set, only include trades with close_reason in (mean_reversion, stop_loss, timeout, stale_quote) — excludes old PaperEngine trades which have close_reason like "spread_converged"

### Implementation notes

- Use only stdlib (sqlite3, argparse, etc.) — no pandas or numpy dependency
- Filter trades: only include those with `close_reason IN ('mean_reversion', 'stop_loss', 'timeout', 'stale_quote')` by default (these are MR engine trades). Old PaperEngine trades have different close reasons ('spread_converged', 'stop_spread', etc.)
- Return on capital = `sum(net_pnl_usdt) / (2 * notional) * 100` — because capital is locked on both legs
- Profit factor = `sum(net_pnl where positive) / abs(sum(net_pnl where negative))`
- Equity curve: group trades by hour, show cumulative PnL
- Sort worst trades by net_pnl ascending

### Files to create

- `scripts/analyze_paper_trades.py`

---

## How to verify

### Part 1 (stale fix):
1. Add `MR_EXIT_MAX_QUOTE_AGE_MS=10000` and `MR_EXCLUDED_EXCHANGES=htx` to `.env`
2. Restart bot, run for 30+ minutes (after baseline warmup)
3. Check that `stale_quote` close rate drops significantly:
   ```bash
   grep "mr close" data/mr_production.log | grep -c "stale_quote"
   grep "mr close" data/mr_production.log | grep -c "mean_reversion"
   ```
4. HTX pairs should NOT appear in `mr open` logs

### Part 2 (stats script):
1. Run: `python scripts/analyze_paper_trades.py --db data/spread_arb.sqlite3 --since 2026-05-14`
2. Verify output matches the format above
3. Numbers should match: `SELECT SUM(net_pnl_usdt) FROM paper_trades WHERE close_reason IN ('mean_reversion','stop_loss','timeout','stale_quote');`
