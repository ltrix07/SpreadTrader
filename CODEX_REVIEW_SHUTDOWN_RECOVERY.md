# Codex Task: Review graceful shutdown + orphan recovery changes for critical bugs

## Goal
Review recent changes to `src/spread_arb/mean_reversion_engine.py` and `src/spread_arb/scanner.py` for critical bugs, logic errors, race conditions, or unintended side effects. Do NOT fix anything — only report findings.

## What was changed and why

We added two safety mechanisms for live trading:

### 1. Graceful shutdown (mean_reversion_engine.py, `shutdown()` method, line ~316)

Previously, `shutdown()` only cancelled pending entry/close tasks and cleared dictionaries. **Open live positions were completely ignored** — they stayed open on exchanges after bot exit, accumulating losses and funding fees with no record in `paper_trades`.

Now, after cancelling pending tasks, shutdown iterates `open_positions_by_symbol` and calls `_close_position(close_reason="shutdown")` for each. This closes positions on exchanges via `execute_spread_exit()` and records them in `paper_trades`.

### 2. Orphan position recovery (mean_reversion_engine.py, `recover_orphan_positions()` method, line ~349)

New method called at startup (scanner.py line ~117). In live mode, it iterates all configured symbols × all exchange clients, calls `get_position()` on each, and if `size != 0`, closes the position with a market order. This catches positions left by a previous crash where graceful shutdown didn't run.

### 3. Summary display update (mean_reversion_engine.py, `log_summary()`)

Added "shutdown" to the close reason counters display.

## What to check

### Critical — must verify:

1. **Race condition in shutdown: pending_closes vs _close_position.** At line 318-324, we cancel all `pending_closes_by_symbol` tasks. Then at line 326-345, we call `_close_position()` for open positions. But `_close_position()` pops from `open_positions_by_symbol` at the end (line ~1083). Check: if a pending close was in progress when we cancelled it, could the position still be in `open_positions_by_symbol`? If yes, `_close_position` would try to close it again — a double close on the exchange. Is this safe?

2. **Race condition in shutdown: asyncio task cancellation timing.** When we `task.cancel()` and `await asyncio.gather(*tasks, return_exceptions=True)`, the CancelledError may fire at any await point inside `_close_position` or `_execute_after_delay`. Check: could a cancelled `_close_position` have already sent the close order to the exchange but not yet written to `paper_trades`? Then our shutdown loop would close again → double fill on exchange.

3. **get_latest_quote callable during shutdown.** Line 337-338 calls `self.get_latest_quote(position.long_exchange, symbol)`. This callable is `scanner._get_latest_quote()` which reads from `scanner.latest_quotes`. By shutdown time, WS feeds are already cancelled (scanner.py line 206-207). Are the quotes still in the dict? They should be (cancelling feeds doesn't clear the dict), but verify.

4. **recover_orphan_positions: closing without knowing the pair.** Recovery closes orphan positions on individual exchanges. But MR positions are spread pairs (long on exchange A, short on exchange B). If we close only one leg (e.g. only Bybit had size != 0 because the other leg was already closed by exchange stop-loss), that's correct. But if BOTH legs are open, we close them independently — not as a spread. Check: is this safe? Could the two independent closes happen at significantly different prices, causing additional slippage?

5. **recover_orphan_positions: symbols list doesn't include dynamic rotation symbols.** Line 355: `symbols = list(self.settings.symbols)`. But orphan positions could be on dynamically rotated symbols from a previous session. Those symbols are NOT in `self.settings.symbols` — they were added by `SymbolRotator`. Check: should we also scan dynamic symbols, or is this an acceptable limitation?

6. **recover_orphan_positions: rate limiting.** The method calls `get_position()` for every symbol × every exchange. With 50 symbols × 5 exchanges = 250 API calls. There's `await asyncio.sleep(0.1)` between symbols (line 393), so 50 × 0.1 = 5 seconds total. But within each symbol, all exchanges are called without delay. Check: is this sufficient, or could exchanges rate-limit the `get_position` calls?

7. **Scanner startup ordering.** Line 117-118: `recover_orphan_positions()` is called BEFORE WS feeds start (line 132). The recovery uses `execution_service.clients` which were initialized at line 101-104. Check: are the clients fully ready (HTTP sessions open, etc.) at this point? `ExecutionService.initialize()` is called at line 105 — verify this sets up HTTP sessions.

8. **shutdown() called but execution_service is None.** In paper mode, `self.live_mode = False`, so the new shutdown block is skipped (guarded by `if self.live_mode`). But what if `live_mode = True` but `execution_service` is somehow None? The `_close_position` method checks this at line 930-932 and returns early with CRITICAL log. Verify this edge case is handled.

### Important but not critical:

9. **Orphan recovery doesn't record in paper_trades.** This is by design (we don't have entry price info), but means these closes still won't appear in trade statistics. Note this as a limitation.

10. **Shutdown timeout.** There's no timeout on the shutdown close loop. If an exchange is down, `execute_spread_exit()` could hang (it has `order_timeout_sec` but not for the whole spread). Check if the signal handler could fire again (double SIGTERM) and what would happen.

## Files to read

- `src/spread_arb/mean_reversion_engine.py` — focus on:
  - `shutdown()` method (line ~316)
  - `recover_orphan_positions()` method (line ~349)
  - `_close_position()` method (line ~920) — understand the close flow
  - `log_summary()` method — verify shutdown reason added

- `src/spread_arb/scanner.py` — focus on:
  - Startup sequence (lines ~108-132) — where recovery is called
  - Shutdown sequence (lines ~190-215) — order of operations

- `src/spread_arb/execution.py` — focus on:
  - `execute_spread_exit()` — understand how close orders work
  - `initialize()` — verify clients are ready before recovery

## Output format

Report as a numbered list:
1. Issue description
2. Severity: CRITICAL / WARNING / INFO
3. Location (file + line range)
4. Suggested fix (one-liner description, do NOT implement)
