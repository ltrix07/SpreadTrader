# Codex Task: Graceful shutdown with live position closure + startup recovery

## Problem

When the bot is killed (SIGTERM, SIGINT, or `pkill`), open live positions on exchanges are abandoned:
- Positions remain open on exchanges, accumulating losses and funding fees
- The `paper_trades` table never gets the closing record (it's written in `_close_position()`)
- Balance dashboard shows losses that don't match recorded trades
- Real example: 8 MRVLUSDT live trades opened on Bybit, bot was killed, trades not in DB, ~$3 unaccounted loss

## Current Architecture

### Signal handling (scanner.py lines 873-892)
```python
def _install_signal_handlers(self) -> None:
    loop = asyncio.get_running_loop()
    def _request_shutdown(sig_name: str) -> None:
        self.log.info("received %s, shutting down", sig_name)
        self.stop()  # just sets stop_event
    for sig in _supported_signals():
        loop.add_signal_handler(sig, lambda s=sig: _request_shutdown(s.name))
```
`self.stop()` sets `self.stop_event` → background tasks see it and exit → `shutdown()` is called.

### MR engine shutdown (mean_reversion_engine.py lines 316-325)
```python
async def shutdown(self) -> None:
    tasks = [pending.task for pending in self.pending_entries_by_symbol.values()]
    tasks.extend(self.pending_closes_by_symbol.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    self.pending_entries_by_symbol.clear()
    self.pending_closes_by_symbol.clear()
    self.log_summary()
```
**BUG**: `open_positions_by_symbol` is completely ignored. Open live positions are never closed on shutdown.

### Scanner shutdown (scanner.py lines 190-215)
After `stop_event` is set:
1. All background tasks are cancelled
2. `mean_reversion_engine.shutdown()` is called
3. `self.live_clients = {}` — clients are discarded

**BUG**: Live clients are cleared AFTER shutdown, so they're available during shutdown. But shutdown doesn't use them.

### Position close flow (mean_reversion_engine.py lines 920-1094)
`_close_position()` handles both paper and live mode:
- Live mode: calls `execution_service.execute_spread_exit()` to close on exchanges, then records to DB
- Paper mode: estimates exit prices from quotes, records to DB
- Both: writes to `paper_trades` table via `self.opportunity_store.insert_paper_trade()`

### Execution service (execution.py)
- `execute_spread_exit()` — closes both legs concurrently via `place_market_order(close=True)`
- `cancel_protective_stops()` — cancels exchange-side stop orders

### Exchange clients (exchanges/base.py)
- `get_position(symbol) -> PositionInfo` — checks current position on exchange
- `PositionInfo` has: `exchange`, `symbol`, `size`, `entry_price`, `unrealized_pnl`, `leverage`

## What to implement

### Part 1: Graceful shutdown — close open positions before exit

Modify `MeanReversionEngine.shutdown()` to close all open live positions before exiting.

**In `mean_reversion_engine.py`**, update `shutdown()`:

```python
async def shutdown(self) -> None:
    # 1. Cancel pending entries (existing logic)
    tasks = [pending.task for pending in self.pending_entries_by_symbol.values()]
    tasks.extend(self.pending_closes_by_symbol.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    self.pending_entries_by_symbol.clear()
    self.pending_closes_by_symbol.clear()

    # 2. NEW: Close all open live positions
    if self.live_mode and self.open_positions_by_symbol:
        self.log.warning(
            "shutdown: closing %d open live position(s): %s",
            len(self.open_positions_by_symbol),
            list(self.open_positions_by_symbol.keys()),
        )
        for symbol, position in list(self.open_positions_by_symbol.items()):
            try:
                await self._close_position(
                    position=position,
                    close_reason="shutdown",
                    long_quote=self.get_latest_quote(position.long_exchange, symbol),
                    short_quote=self.get_latest_quote(position.short_exchange, symbol),
                )
            except Exception as exc:
                self.log.critical(
                    "shutdown: FAILED to close %s | %s | POSITION MAY BE ORPHANED",
                    symbol, exc,
                )

    self.log_summary()
```

**Important details:**
- Use `close_reason="shutdown"` — this is a new reason, add it to the `log_summary` counters display (line ~272-277)
- `get_latest_quote` is the callable stored in `self.get_latest_quote` — it returns the last known quote, may be stale but that's fine for emergency close
- Iterate over `list(...)` because `_close_position` pops from the dict
- The `_close_position` method already handles both live execution and DB recording
- If close fails, log CRITICAL but continue to next position — don't crash

### Part 2: Startup recovery — detect orphan positions

Add a method to `MeanReversionEngine` that checks exchanges for orphan positions from a previous run.

**New method `recover_orphan_positions()`:**

```python
async def recover_orphan_positions(self) -> None:
    """Check exchanges for positions left by a previous bot run and close them."""
    if not self.live_mode or self.execution_service is None:
        return

    self.log.info("checking for orphan positions on exchanges...")
    symbols = list(self.settings.symbols)
    orphans_found = 0

    for symbol in symbols:
        for exchange_name, client in self.execution_service.clients.items():
            try:
                pos = await client.get_position(symbol)
                if abs(float(pos.size)) > 0:
                    orphans_found += 1
                    self.log.warning(
                        "orphan position found | %s on %s | size=%s | entry=%s | upnl=%s",
                        symbol, exchange_name.value, pos.size, pos.entry_price, pos.unrealized_pnl,
                    )
                    # Close the orphan position
                    side = "sell" if float(pos.size) > 0 else "buy"
                    qty = abs(pos.size)
                    try:
                        result = await client.place_market_order(symbol, side, qty, close=True)
                        self.log.info(
                            "orphan closed | %s on %s | filled=%s @ %s | fee=%s",
                            symbol, exchange_name.value, result.filled_qty, result.avg_price, result.fee,
                        )
                    except Exception as close_exc:
                        self.log.critical(
                            "FAILED to close orphan | %s on %s | %s | MANUAL INTERVENTION REQUIRED",
                            symbol, exchange_name.value, close_exc,
                        )
            except Exception as exc:
                self.log.debug("could not check %s on %s: %s", symbol, exchange_name.value, exc)

    if orphans_found == 0:
        self.log.info("no orphan positions found")
    else:
        self.log.warning("closed %d orphan position(s)", orphans_found)
```

**Call it from `scanner.py`** after initializing the MR engine and execution service (line ~116, after `preload_baselines`):

```python
self.mean_reversion_engine.preload_baselines(self.settings.database_url)
if self.settings.live_trading and execution_service is not None:
    await self.mean_reversion_engine.recover_orphan_positions()
```

**Important details:**
- Only runs in live mode
- Iterates ALL configured symbols × ALL exchange clients
- For each, calls `get_position()` — if size != 0, it's an orphan
- Closes with a market order on the same exchange
- Logs everything for audit trail
- Does NOT record in `paper_trades` (we don't have entry price info for a proper PnL calc — the orphan was from a previous run)
- If close fails → CRITICAL log, continue to next position
- Rate limiting: exchanges may rate-limit `get_position` calls. Add a small `await asyncio.sleep(0.1)` between symbols to be safe

### Part 3: Add "shutdown" close reason to summary display

In `log_summary()` (mean_reversion_engine.py line ~272), add shutdown to the reason_parts:
```python
reason_parts = [
    f"mean_reversion={self.close_reason_counts.get('mean_reversion', 0)}",
    f"stop_loss={self.close_reason_counts.get('stop_loss', 0)}",
    f"timeout={self.close_reason_counts.get('timeout', 0)}",
    f"stale_quote={self.close_reason_counts.get('stale_quote', 0)}",
    f"shutdown={self.close_reason_counts.get('shutdown', 0)}",
]
```

## Files to modify

1. `src/spread_arb/mean_reversion_engine.py`:
   - Modify `shutdown()` method (line ~316) — add live position closing
   - Add `recover_orphan_positions()` method after `shutdown()`
   - Update `log_summary()` (line ~272) — add "shutdown" reason

2. `src/spread_arb/scanner.py`:
   - Add `recover_orphan_positions()` call after `preload_baselines` (line ~116)

## What NOT to change

- Do NOT modify `_close_position()` method
- Do NOT modify `_install_signal_handlers()` — current signal handling is fine, it sets stop_event which leads to shutdown()
- Do NOT modify `ExecutionService`
- Do NOT modify exchange clients
- Do NOT add new config parameters — this should always be enabled for live mode

## Testing

1. Verify `shutdown()` with open positions:
   - Create a mock open position in `open_positions_by_symbol`
   - Call `shutdown()`
   - Verify `_close_position` was called with `close_reason="shutdown"`
   - Verify position is removed from `open_positions_by_symbol`

2. Verify `shutdown()` without open positions:
   - Call `shutdown()` with empty `open_positions_by_symbol`
   - Verify no errors, just `log_summary()` called

3. Verify `recover_orphan_positions()`:
   - Mock `get_position` to return a position with size > 0
   - Verify `place_market_order(close=True)` is called
   - Verify CRITICAL log on close failure

4. Syntax check: `python -m py_compile src/spread_arb/mean_reversion_engine.py src/spread_arb/scanner.py`

## Expected result

- On SIGTERM/SIGINT: bot closes all open positions on exchanges, records them in `paper_trades` with reason="shutdown", then exits
- On startup: bot checks all exchanges for orphan positions from previous run, closes them
- No more "ghost" losses that don't appear in trade statistics
