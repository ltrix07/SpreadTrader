# Task: Fix event loop starvation that prevents periodic snapshot collector from running

## Problem

The `_collect_spread_snapshots()` coroutine in `src/spread_arb/scanner.py` is designed to run every 10 seconds, collecting spread data across all symbol × exchange-pair combinations and writing them to the `spread_snapshots` SQLite table. In practice it runs far less often — approximately 59 times in 2 hours instead of the expected 720 (once per 10 seconds).

The root cause is event loop starvation: the `_on_quote()` callback processes every WebSocket message synchronously before yielding, and at 6 exchanges × 50 symbols the message rate is hundreds per second. The event loop never gets a chance to schedule the periodic `_collect_spread_snapshots` task.

## Architecture overview

```
WebSocket feeds (6 exchanges, each in its own asyncio task)
  └── base.py: _read_loop() — `async for msg in ws` → calls on_quote(quote)
        └── scanner.py: _on_quote(quote) — stores quote, then calls _scan_symbol()
              └── _scan_symbol() — iterates combinations(exchanges, 2) for that symbol
                    └── _evaluate_direction() × 2 per pair — computes spread, classifies
                    └── _record_opportunity() — synchronous sqlite3 INSERT + conn.commit()

Periodic tasks (all in the same event loop):
  - _collect_spread_snapshots() — every 10s, iterates all quotes, writes to spread_snapshots table
  - _log_top_spreads() — every N seconds
  - _log_quote_health() — every 10s
  - _monitor_stale_quotes() — periodic
  - paper_engine.summary_loop() — periodic
```

## The hot path that causes starvation

In `base.py` `_read_loop` (line 178-232):
```python
async for msg in ws:
    ...
    quote = self._parse_message(raw)
    if quote is not None:
        callback_result = self.on_quote(quote)
        if isinstance(callback_result, Awaitable):
            await callback_result
```

This calls `_on_quote` for every WS message. `_on_quote` (scanner.py line 119):
```python
async def _on_quote(self, quote: Quote) -> None:
    key = (quote.exchange, quote.symbol)
    self.last_quote_at[key] = quote.received_at
    self.latest_quotes[key] = quote
    self._scan_symbol(quote.symbol)          # <-- HEAVY synchronous work
    if self.paper_engine is not None:
        self.paper_engine.on_quote_tick()     # <-- more synchronous work
    await asyncio.sleep(0)                   # <-- attempted fix, insufficient
```

`_scan_symbol` (line 130) is fully synchronous:
1. Filters `self.latest_quotes` by symbol → O(total_quotes)
2. For each pair in `combinations(exchanges, 2)`:
   - Calls `_evaluate_direction()` twice (forward + reverse)
   - Each call computes spreads using `Decimal` arithmetic
   - If `raw_spread_pct >= min_raw_spread_pct`, calls `_record_opportunity()`
3. `_record_opportunity()` does a synchronous `sqlite3` INSERT + `conn.commit()` — blocking I/O in the event loop

With 6 exchanges and quotes for the same symbol arriving from all 6 in rapid succession, `_scan_symbol` runs C(6,2)=15 pair evaluations × 2 directions = 30 `_evaluate_direction` calls PER quote. At hundreds of quotes/sec, the event loop is monopolized.

The `await asyncio.sleep(0)` at the end of `_on_quote` yields once per quote, but if there are already hundreds of messages buffered in the aiohttp WS receive queue, the `async for msg in ws` loop immediately picks up the next one. The periodic tasks only get scheduled when the WS receive buffer is empty, which may never happen for extended periods.

## Evidence

- Bot ran for 2 hours
- Expected snapshot cycles: `2 * 3600 / 10 = 720`
- Actual snapshot cycles: `24838 total rows / 420 unique pairs ≈ 59 cycles`
- That's about 8% of the expected rate
- Previous attempt (without `await asyncio.sleep(0)`): only 8 cycles in 7 hours

## What works correctly

The `_collect_spread_snapshots()` method itself is correct — when it gets CPU time, it properly:
- Copies `self.latest_quotes` via `dict()` to avoid iteration errors
- Groups quotes by symbol
- Generates all exchange pair combinations
- Filters stale quotes (>30s)
- Normalizes pair ordering (exchange_a < exchange_b alphabetically)
- Batch-inserts via `executemany` + single `commit`

The problem is purely that it doesn't get scheduled often enough.

## Requirements for the fix

1. `_collect_spread_snapshots()` MUST run reliably every ~10 seconds regardless of WebSocket message volume
2. No data races on `self.latest_quotes` — the dict is written by WS callbacks and read by the snapshot collector
3. Existing opportunity detection logic (`_scan_symbol`, `_evaluate_direction`, `_record_opportunity`) must continue working
4. Paper trading engine (`self.paper_engine.on_quote_tick()`) must continue working
5. All existing periodic tasks (`_log_top_spreads`, `_log_quote_health`, `_monitor_stale_quotes`, `paper_engine.summary_loop`) should also benefit from the fix
6. Don't break the graceful shutdown flow (`stop_event`, task cancellation)

## Suggested approaches (pick one or combine)

### Option A: Run snapshot collector in a separate thread

Use `threading.Thread` or `asyncio.to_thread` for the snapshot collector. Since it only reads from `self.latest_quotes` (via a `dict()` copy) and writes to its own SQLite connection, it's naturally thread-safe.

```python
# In run():
import threading
snapshot_thread = threading.Thread(
    target=self._collect_spread_snapshots_sync,
    daemon=True,
)
snapshot_thread.start()
```

This guarantees it runs on its own schedule regardless of event loop load.

### Option B: Throttle/debounce _scan_symbol calls

Instead of scanning on every quote, batch quotes and scan periodically:

```python
async def _on_quote(self, quote: Quote) -> None:
    key = (quote.exchange, quote.symbol)
    self.last_quote_at[key] = quote.received_at
    self.latest_quotes[key] = quote
    # Don't call _scan_symbol here — let a periodic task do it
```

Then add a periodic task that runs `_scan_symbol` for all symbols every 0.5-1 seconds. This dramatically reduces CPU usage in the event loop and gives other tasks time to run.

### Option C: Move SQLite writes to a thread executor

The `_record_opportunity()` call does blocking I/O (`conn.commit()`). Moving it to `loop.run_in_executor()` would free the event loop during DB writes:

```python
def _record_opportunity(self, opportunity: SpreadOpportunity) -> None:
    # ... build record ...
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, self.opportunity_store.insert_opportunity, record)
```

### Option D: Yield more aggressively in the WS read loop

In `base.py` `_read_loop`, add periodic yields:

```python
async for msg in ws:
    self._msg_count_since_yield += 1
    if self._msg_count_since_yield >= 50:  # yield every 50 messages
        await asyncio.sleep(0)
        self._msg_count_since_yield = 0
    ...
```

This is the simplest change but may not fully solve the problem if the WS buffer refills faster than it drains.

### Recommended: Combine B + C

Debouncing `_scan_symbol` (Option B) gives the biggest win because it eliminates the main CPU hog. Moving SQLite writes to a thread (Option C) removes the remaining blocking I/O. Together they should free the event loop completely.

## Files to modify

- `src/spread_arb/scanner.py` — main changes (throttle _scan_symbol, thread for DB writes or snapshots)
- `src/spread_arb/ws_feeds/base.py` — optional (yield in read loop)
- `src/spread_arb/storage.py` — no changes needed (already correct)

## How to verify the fix

1. Run the bot for 10 minutes
2. Count snapshot cycles: `SELECT COUNT(DISTINCT timestamp) FROM spread_snapshots;`
3. Expected: ~60 distinct timestamps (one per 10 seconds)
4. Also check the log for the periodic message: `spread snapshots | cycle=... batch=... total=...`
   - `cycle` should increment by 6 every ~60 seconds (logged every 6th cycle)
5. Ensure opportunities are still being detected: `SELECT COUNT(*) FROM opportunities;` should still grow

## Testing

Run existing tests (if any) with `pytest`. The snapshot collector logic itself doesn't need changes — only the scheduling mechanism. Focus testing on:
- Bot starts and stops cleanly
- Snapshot cycles match wall-clock time
- Opportunities still detected
- No thread-safety crashes or data corruption
