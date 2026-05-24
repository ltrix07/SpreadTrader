# Codex Task: Fast baseline warmup for new/rotated symbols

## Problem

When the bot starts or dynamic rotation adds new symbols, MR baselines need 360 samples to become "ready" (`RollingBaseline.is_ready`). Baselines update via `_collect_spread_snapshots()` which runs every 10 seconds. So warmup takes 360 × 10s = 60 minutes minimum, ~2 hours in practice.

Dynamic rotation runs 3× daily. Each rotation adds/removes symbols and new symbols start with empty baselines. This means ~6 hours/day of downtime where the bot can't trade rotated symbols.

## Current Architecture

### Baseline update flow

1. `scanner.py` line 510: `_collect_spread_snapshots()` — async loop, runs every 10 seconds
2. Line 543: calls `self.mean_reversion_engine.update_baselines(snapshot_quotes)`
3. `mean_reversion_engine.py` line 328: `update_baselines()` — iterates ALL symbols × ALL exchange pairs, computes directional spreads, calls `_update_baseline()` for each direction
4. `_update_baseline()` appends to `RollingBaseline.window` (deque of size `mr_rolling_window=360`)
5. `RollingBaseline.is_ready` returns `True` when `len(window) >= window_size`

### Startup preload (already exists)

`mean_reversion_engine.py` line 167: `preload_baselines(database_url)` — loads recent rows from `spread_snapshots` table in SQLite, feeds them into `_update_baseline()`. Called once at startup (scanner.py line 116). This fills baselines from historical data so startup warmup is fast IF there's recent data in the DB.

**Important**: `preload_baselines()` loads ALL symbols from the last `window × 700` rows. It does NOT filter by symbol. It processes rows in chronological order (oldest first via `reversed(rows)`).

### Dynamic rotation flow

1. `symbol_rotator.py` line 371: `_do_rotation()` — calls `discover_candidates()` to find new symbols via REST snapshots
2. Line 405: calls `on_symbols_changed(added, removed)`
3. `scanner.py` line 722: `_on_dynamic_symbols_changed()`:
   - For added symbols: subscribes to WS feeds (line 726)
   - For removed symbols: unsubscribes WS, cleans up quotes, **deletes baselines** (lines 760-770)
   - **For added symbols: does NOT preload baselines** — this is the gap

### Spread snapshots table schema

The `spread_snapshots` table has columns:
- `timestamp` (ISO string)
- `symbol`
- `exchange_a`, `exchange_b` (alphabetically ordered)
- `raw_spread_ab_pct`, `raw_spread_ba_pct`
- `bid_a`, `ask_a`, `bid_b`, `ask_b`
- `quote_age_a_ms`, `quote_age_b_ms`

Records are inserted every 10 seconds by `_collect_spread_snapshots()`.

## What to implement

### 1. Add `preload_baselines_for_symbols()` method to `MeanReversionEngine`

Create a new method similar to `preload_baselines()` but:
- Takes a `symbols: list[str]` parameter
- Only loads data for those specific symbols (WHERE clause)
- Loads `mr_rolling_window` rows per symbol-pair (not `window × 700` for everything)
- Sets `self._preloading = True` during execution to suppress signal evaluation
- Uses the same `_update_baseline()` mechanism

Signature:
```python
def preload_baselines_for_symbols(self, database_url: str, symbols: list[str]) -> None:
```

The SQL should filter by symbol:
```sql
SELECT symbol, exchange_a, exchange_b, raw_spread_ab_pct, raw_spread_ba_pct
FROM spread_snapshots
WHERE symbol IN (?, ?, ...)
ORDER BY timestamp DESC
LIMIT ?
```

Limit should be `len(symbols) × 20 × window` (20 = max exchange pairs per symbol: 5 exchanges × 4 / 2 = 10 pairs × 2 for safety).

Process rows in reverse order (oldest first) just like `preload_baselines()`.

### 2. Call it from `_on_dynamic_symbols_changed()` in `scanner.py`

After subscribing new symbols to WS feeds (line 726-734), call the preload:
```python
if added and self.mean_reversion_engine is not None:
    self.mean_reversion_engine.preload_baselines_for_symbols(
        self.settings.database_url, added
    )
```

This is synchronous (blocking) — that's fine because:
- Rotation already pauses signal evaluation (`self.scanning = True`)
- It runs infrequently (3× daily)
- The SQLite read should take < 1 second

### 3. Preload on startup for ALL symbols (optimization)

The existing `preload_baselines()` method works but loads `window × 700` rows regardless of how many symbols we have. With 50 symbols this is 252,000 rows — potentially slow.

Optimize it: calculate the actual limit as `num_unique_pairs × window` where `num_unique_pairs = len(symbols) × C(num_exchanges, 2)`. Cap at current value as maximum.

This is a nice-to-have, not critical.

## What NOT to change

- Do NOT change `RollingBaseline` class
- Do NOT change `_update_baseline()` method
- Do NOT change `_collect_spread_snapshots()` timing or logic
- Do NOT change `update_baselines()` method
- Do NOT add async to `preload_baselines_for_symbols()` — keep it sync like `preload_baselines()`
- Do NOT modify the `SymbolRotator` class

## Files to modify

1. `src/spread_arb/mean_reversion_engine.py` — add `preload_baselines_for_symbols()` method after existing `preload_baselines()` (line ~246)
2. `src/spread_arb/scanner.py` — add preload call in `_on_dynamic_symbols_changed()` after WS subscribe block (line ~734)

## Testing

After implementation, verify:
1. `preload_baselines_for_symbols(url, ["BTCUSDT"])` loads baselines for BTCUSDT pairs only
2. After preload, `baseline.is_ready` returns True for the loaded pairs (if enough historical data exists)
3. `_on_dynamic_symbols_changed(["NEWCOIN"], [])` triggers preload for NEWCOIN
4. The `_preloading = True` flag suppresses signal evaluation during preload
5. Symbols NOT in the `symbols` list are NOT affected

## Expected result

After rotation adds new symbols, their baselines should be ready within seconds (from DB preload) instead of 60+ minutes. This eliminates the warmup downtime during rotation.

If there's no historical data for a new symbol (first time seen), it will still need live warmup — that's expected and acceptable.
