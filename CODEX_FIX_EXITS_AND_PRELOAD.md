# Task: Fix MR exit logic + add take-profit + preload baselines from DB

## Context

The MeanReversionEngine (`src/spread_arb/mean_reversion_engine.py`) is live but all trades are closing with `stale_quote` instead of `mean_reversion`. Two root causes and one UX problem need fixing.

Current paper trading results (6 trades):
- 0 closed by `mean_reversion` (the desired outcome)
- 6 closed by `stale_quote` (forced exit)
- Net PnL: -$3.31
- Even profitable trades (+$1.36 XRPUSDT) were profitable by luck at stale close, not by mean reversion

## Fix 1: Increase exit stale threshold to 30 seconds

### Problem
`mr_exit_max_quote_age_ms=10000` (10s) is too strict. WebSocket feeds don't send updates continuously — less liquid pairs can go 10+ seconds without a trade, causing quote age to exceed the threshold. The bot enters on a fresh quote, holds for 1-4 minutes, then quotes go stale and position is force-closed before mean reversion happens.

### Fix
In `src/spread_arb/config.py`, change the default:

```python
mr_exit_max_quote_age_ms: int = Field(default=30_000, ge=1)  # was 10_000
```

30 seconds is safe for perps — prices don't move catastrophically in 30s, and this gives the position enough time to stay open through natural gaps in quote flow.

### Files
- `src/spread_arb/config.py` — change default value

## Fix 2: Add partial take-profit exit

### Problem
Current exit condition for mean reversion is:
```python
if current_spread <= position.entry_rolling_mean:
    close_reason = "mean_reversion"
```

This waits for FULL reversion to the rolling mean. If entry spread is +0.5% and mean is -0.05%, the spread needs to move 0.55 percentage points. This is too greedy — the spread may partially revert (capturing most of the edge) but never reach the mean before timing out or going stale.

### Fix
Add a configurable take-profit fraction. Instead of waiting for full reversion, exit when a fraction of the edge is captured.

**New config parameter in `src/spread_arb/config.py`:**
```python
mr_take_profit_fraction: float = Field(default=0.5, gt=0, le=1.0)
# 0.5 = exit when 50% of (entry_spread - rolling_mean) edge is captured
# 1.0 = wait for full reversion to mean (current behavior)
```

**New exit logic in `check_exits()` of `src/spread_arb/mean_reversion_engine.py`:**

Replace:
```python
if current_spread_pct <= position.entry_rolling_mean:
    close_reason = "mean_reversion"
```

With:
```python
edge_at_entry = position.entry_spread_pct - position.entry_rolling_mean
take_profit_target = position.entry_spread_pct - (edge_at_entry * self.settings.mr_take_profit_fraction)
if current_spread_pct <= take_profit_target:
    close_reason = "mean_reversion"
```

Example: entry_spread=+0.50%, mean=-0.05%, edge=0.55%, fraction=0.5
→ target = 0.50 - (0.55 * 0.5) = 0.50 - 0.275 = +0.225%
→ exit when spread drops to +0.225% instead of -0.05%

This captures 50% of the edge faster and more reliably, reducing exposure to stale_quote and timeout closes.

**Also log the target on open** so we can see what the bot is aiming for:
```python
self.log.info(
    "mr open | %s | long=%s @ %.6f | short=%s @ %.6f | spread=%+.4f%% | mean=%+.4f%% | sigma=%.2f | target=%+.4f%%",
    ...
    take_profit_target,
)
```

Store the target in `MeanRevPosition` for reference:
```python
@dataclass(slots=True)
class MeanRevPosition:
    ...
    take_profit_target: float  # the spread level we're aiming for
```

### Files
- `src/spread_arb/config.py` — add `mr_take_profit_fraction`
- `src/spread_arb/mean_reversion_engine.py` — add field to `MeanRevPosition`, compute target in `_execute_after_delay`, use target in `check_exits`

## Fix 3: Preload baselines from database on startup

### Problem
Rolling baselines are stored in memory. Every time the bot restarts, baselines reset to zero and need 60 minutes (360 snapshots × 10s) to warm up. This wastes an hour after every restart/deploy.

### Fix
On engine initialization, load the last `mr_rolling_window` snapshots per directional pair from the `spread_snapshots` table and pre-populate the baselines.

**New method in `MeanReversionEngine`:**

```python
def preload_baselines(self, database_url: str) -> None:
    """Load recent spread snapshots from DB to pre-populate rolling baselines."""
    import sqlite3
    from .storage import _sqlite_path_from_url
    
    db_path = _sqlite_path_from_url(database_url)
    if not db_path.exists():
        self.log.warning("preload: database not found at %s", db_path)
        return
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Check if table exists
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='spread_snapshots'"
        ).fetchone()
        if not has_table:
            self.log.info("preload: spread_snapshots table not found, starting cold")
            return
        
        # Get the last N timestamps (N = rolling_window)
        # We need the most recent `window_size` snapshots per pair
        window = self.settings.mr_rolling_window
        
        # Get all recent snapshots ordered by timestamp
        # Limit to reasonable amount: window_size * estimated_max_pairs
        max_rows = window * 700  # 50 symbols * ~12 pairs each ≈ 600, round up
        rows = conn.execute(
            """
            SELECT symbol, exchange_a, exchange_b,
                   raw_spread_ab_pct, raw_spread_ba_pct
            FROM spread_snapshots
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (max_rows,)
        ).fetchall()
        
        if not rows:
            self.log.info("preload: no snapshots found, starting cold")
            return
        
        # Rows are in reverse chronological order, we need chronological
        rows.reverse()
        
        # Feed each directional spread into baselines
        loaded_count = 0
        for r in rows:
            symbol = r["symbol"]
            ex_a = r["exchange_a"]
            ex_b = r["exchange_b"]
            
            # Map string exchange names back to ExchangeName enum
            try:
                ex_a_enum = ExchangeName(ex_a)
                ex_b_enum = ExchangeName(ex_b)
            except ValueError:
                continue
            
            # Direction A->B (long A, short B)
            self._update_baseline(
                symbol=symbol,
                long_exchange=ex_a_enum,
                short_exchange=ex_b_enum,
                spread_pct=float(r["raw_spread_ab_pct"]),
            )
            # Direction B->A (long B, short A)
            self._update_baseline(
                symbol=symbol,
                long_exchange=ex_b_enum,
                short_exchange=ex_a_enum,
                spread_pct=float(r["raw_spread_ba_pct"]),
            )
            loaded_count += 1
        
        ready_count = len(self.baseline_ready_keys)
        total_baselines = len(self.baselines)
        self.log.info(
            "preload complete | loaded %d snapshots | %d baselines total | %d ready",
            loaded_count, total_baselines, ready_count,
        )
    finally:
        conn.close()
```

**Call it in scanner.py after creating the engine:**

In `QuoteScanner.run()`, after creating `MeanReversionEngine`, add:
```python
if self.mean_reversion_engine is not None:
    self.mean_reversion_engine.preload_baselines(self.settings.database_url)
```

**Important:** The `_update_baseline` method already handles window creation, updates, and logging "mr baseline ready". So preloading through it will automatically trigger the ready messages and set `baseline_ready_keys`.

**Suppress "mr baseline ready" log spam during preload** — during preload, hundreds of baselines become ready at once. Add a flag:

```python
def preload_baselines(self, database_url: str) -> None:
    self._preloading = True
    try:
        # ... load and update ...
    finally:
        self._preloading = False
    # Log summary instead of individual ready messages
```

In `_update_baseline`, check:
```python
if not was_ready and baseline.is_ready and key not in self.baseline_ready_keys:
    self.baseline_ready_keys.add(key)
    if not getattr(self, '_preloading', False):
        self.log.info("mr baseline ready | ...")
```

### Files
- `src/spread_arb/mean_reversion_engine.py` — add `preload_baselines()` method, suppress log during preload
- `src/spread_arb/scanner.py` — call `preload_baselines()` after engine creation

## Summary of all changes

| File | Change |
|------|--------|
| `src/spread_arb/config.py` | Change `mr_exit_max_quote_age_ms` default to 30000. Add `mr_take_profit_fraction` (default 0.5). |
| `src/spread_arb/mean_reversion_engine.py` | Use take-profit target in `check_exits()`. Add `take_profit_target` field to `MeanRevPosition`. Compute target in `_execute_after_delay`. Add `preload_baselines()` method. Suppress baseline-ready spam during preload. |
| `src/spread_arb/scanner.py` | Call `preload_baselines(settings.database_url)` after MR engine creation. |

## How to verify

1. Restart bot — should see "preload complete | loaded N snapshots | M baselines total | K ready" in first few seconds
2. Should NOT see 1200+ individual "mr baseline ready" messages
3. MR signals and trades should start immediately (no 60-minute warmup)
4. After 30+ minutes of trading, run:
   ```bash
   python scripts/analyze_paper_trades.py --db data/spread_arb.sqlite3 --since <restart_time>
   ```
5. Check that `mean_reversion` close reason appears (not just `stale_quote`)
6. Check that `stale_quote` percentage is significantly lower than before

## .env updates after deploy
```
MR_EXIT_MAX_QUOTE_AGE_MS=30000
MR_TAKE_PROFIT_FRACTION=0.5
```
