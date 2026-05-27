# Code Audit Report - 2026-05-27

## Summary
The audit found 2 critical, 5 high, 5 medium, and 2 low issues. The codebase is structurally coherent, but the live-trading path still has several order-state and exchange-abstraction defects that can create orphaned positions, unprotected exposure, or materially wrong signal filtering.

## Critical

### 1. Live entry can leave a real position open but untracked
- **Title** - Post-fill exceptions during entry return before local position state is created
- **Location** - `src/spread_arb/mean_reversion_engine.py:1051-1079`
- **What** - The live entry `try` block covers both `execute_spread_entry()` and `place_protective_stops()`. If the spread entry succeeds but any later step throws, the code logs `LIVE entry failed` and returns before `MeanRevPosition` is created and stored in `open_positions_by_symbol`.
- **Why it matters** - A real two-leg position can already exist on exchanges while the bot forgets about it entirely. That disables normal exit handling and turns the position into an orphan until manual intervention or a later restart recovery.
- **Suggested action** - Persist local position state immediately after confirmed fills, then handle stop placement failure as a separate tracked error path.

### 2. Entry/exit timeouts do not reconcile exchange state before assuming failure
- **Title** - Order timeout handling can orphan live legs after local coroutine cancellation
- **Location** - `src/spread_arb/execution.py:138-148`, `src/spread_arb/execution.py:195-206`
- **What** - Both spread entry and spread exit are wrapped in `asyncio.wait_for(...)`. On timeout, the code raises `ExecutionError` immediately without querying either exchange for resulting fills or open positions.
- **Why it matters** - A timed-out HTTP/WebSocket request does not mean the exchange rejected the order. Either leg may have filled after the local timeout, leaving real exposure while the bot assumes the operation failed.
- **Suggested action** - On timeout, explicitly query both exchanges for order/position state and run a deterministic recovery path before returning failure.

## High

### 3. Protective stop failures are treated as non-fatal
- **Title** - Live positions can open with one or both exchange-side stops missing
- **Location** - `src/spread_arb/execution.py:245-289`, `src/spread_arb/mean_reversion_engine.py:1068-1077`
- **What** - `place_protective_stops()` logs and suppresses exceptions, returns empty stop IDs, and the engine still opens the position normally.
- **Why it matters** - The bot can hold live exposure without the catastrophic exchange-side protection it assumes exists. A partial failure is especially dangerous because only one leg may be protected.
- **Suggested action** - Treat missing stops as a tracked failure state and either close the fresh position immediately or keep it in a degraded-but-explicit state.

### 4. Gate and OKX quote sizes are used in the wrong unit for liquidity checks
- **Title** - Contract-count book sizes are treated as base-asset sizes
- **Location** - `src/spread_arb/exchanges/gate.py:86-108`, `src/spread_arb/exchanges/gate.py:244-261`, `src/spread_arb/ws_feeds/gate_ws.py:165-194`, `src/spread_arb/exchanges/okx.py:105-125`, `src/spread_arb/exchanges/okx.py:185-200`, `src/spread_arb/ws_feeds/okx_ws.py:115-143`, `src/spread_arb/scanner.py:320-325`, `src/spread_arb/mean_reversion_engine.py:797-805`, `src/spread_arb/mean_reversion_engine.py:930-956`
- **What** - Gate and OKX order paths explicitly convert between contracts and base quantity using `quanto_multiplier` / `ctVal`, but quote ingestion stores raw book sizes directly into `Quote.best_*_size`. Liquidity filters later multiply price by those raw sizes as if they were base units.
- **Why it matters** - Signal acceptance and revalidation use incorrect top-of-book capacity. On symbols where contract multipliers are not `1`, the bot can reject valid trades, accept thin-book trades, or distort baseline diagnostics. This can directly suppress trades on the configured symbol set.
- **Suggested action** - Normalize Gate and OKX book sizes into canonical base units at quote-ingestion time, in both REST and WS feeds.

### 5. Quantity rounding uses minimum order size as if it were lot step size
- **Title** - Order quantities are rounded by `minQty`, not by the venue's actual increment
- **Location** - `src/spread_arb/execution.py:314-329`, `src/spread_arb/exchanges/base.py:82`
- **What** - `_round_qty()` fills `_step_size_cache` by calling `get_min_order_qty()`. The interface only exposes minimum quantity, not step size, so the executor rounds by the wrong market attribute.
- **Why it matters** - Many venues distinguish minimum size from lot increment. This can round orders to invalid quantities, to zero, or to larger-than-needed chunks, causing live rejections or mis-sized hedges.
- **Suggested action** - Split exchange metadata into separate `min_qty` and `step_size` retrieval and round only by the true step size.

### 6. Baseline preload is globally capped instead of loading a full window per pair
- **Title** - Restart warmup can leave baselines cold even when the database already has enough history
- **Location** - `src/spread_arb/mean_reversion_engine.py:193-199`, `src/spread_arb/mean_reversion_engine.py:199-218`, `src/spread_arb/mean_reversion_engine.py:258-279`
- **What** - Preload reads a single `ORDER BY timestamp DESC LIMIT ?` slice across the whole `spread_snapshots` table. The cap is global, not per symbol/pair, so active pairs compete with every other pair in the database for the same row budget.
- **Why it matters** - On larger universes, some baselines never receive `mr_rolling_window` samples after restart even though the DB contains enough total history. That directly prevents `baseline.is_ready` and blocks all signals for affected pairs.
- **Suggested action** - Query the latest `mr_rolling_window` snapshots per directional pair or per `(symbol, exchange_a, exchange_b)` group instead of using one global limit.

### 7. Dynamic WS symbol changes are dropped if the socket is reconnecting
- **Title** - Symbol subscriptions mutate only the live socket, not durable feed state
- **Location** - `src/spread_arb/ws_feeds/base.py:83-109`
- **What** - `subscribe_symbols()` and `unsubscribe_symbols()` return immediately when `_ws` is `None` or closed. In that case they do not update `self.symbols`, so the next reconnect resubscribes the old symbol set.
- **Why it matters** - During dynamic rotation, added symbols can be silently lost and removed symbols can come back after reconnect. That produces stale coverage and missing baselines without crashing the process.
- **Suggested action** - Update `self.symbols` regardless of current socket state, then best-effort apply the delta to the active connection if one exists.

## Medium

### 8. REST polling never picks up dynamically rotated symbols
- **Title** - Dynamic symbol updates only reconfigure WS feeds
- **Location** - `src/spread_arb/scanner.py:133-142`, `src/spread_arb/scanner.py:724-767`, `src/spread_arb/scanner.py:786-800`
- **What** - REST polling tasks are created once from `self.settings.symbols`, and `_on_dynamic_symbols_changed()` only updates WebSocket feeds. REST fallback exchanges never subscribe to added symbols or drop removed ones.
- **Why it matters** - With `use_websocket=false` or with REST-fallback venues, dynamic rotation produces an inconsistent universe: some exchanges track the rotated symbols and others do not.
- **Suggested action** - Rebuild or reconfigure REST pollers when the active symbol set changes.

### 9. Stop-triggered live exits record synthetic prices instead of real fills
- **Title** - Exchange-stop closures can write materially wrong PnL to `paper_trades`
- **Location** - `src/spread_arb/mean_reversion_engine.py:1181-1205`, `src/spread_arb/mean_reversion_engine.py:1243-1282`
- **What** - If a normal exit order fails but both positions are already flat, the code assumes an exchange stop triggered and derives exit prices from the latest quotes, not from the actual stop fill details.
- **Why it matters** - Trade records, win rate, and per-symbol PnL can drift materially from real exchange outcomes, especially during fast moves where stops fill far from the current top of book.
- **Suggested action** - Query exchange order history or trigger-order detail and record the real exit fill before writing the trade record.

### 10. `PaperEngine` undercharges fees on most exchanges
- **Title** - Paper-mode fee table only covers MEXC and Bybit
- **Location** - `src/spread_arb/paper_engine.py:149-152`, `src/spread_arb/paper_engine.py:267-315`, `src/spread_arb/paper_engine.py:382-390`
- **What** - `PaperEngine.exchange_fees_pct` only initializes entries for `MEXC` and `BYBIT`. All other venues default to `0.0` through `.get(..., 0.0)`.
- **Why it matters** - If `PaperEngine` is used, net spread checks and PnL are overstated for Binance, OKX, Gate, Bitget, and HTX.
- **Suggested action** - Populate the full exchange fee table or reuse the same helper used by `QuoteScanner` and `MeanReversionEngine`.

### 11. PnL calculations ignore funding entirely
- **Title** - Reported PnL and timeout estimates omit realized funding transfers
- **Location** - `src/spread_arb/paper_engine.py:109-123`, `src/spread_arb/mean_reversion_engine.py:647-675`, `src/spread_arb/mean_reversion_engine.py:1243-1282`
- **What** - `calculate_pnl()` hardcodes `funding_usdt = 0.0`, and both live and paper trade records use that result. The engine has a pre-entry funding filter, but no post-trade accounting for actual funding paid/received.
- **Why it matters** - Trades that cross a funding event will have wrong reported net PnL, wrong symbol attribution, and potentially misleading timeout decisions based on estimated exit PnL.
- **Suggested action** - Include realized or estimated funding in PnL accounting when hold time overlaps funding windows.

### 12. In-memory baselines can advance even when snapshot persistence fails
- **Title** - Baseline state and database history can diverge after snapshot insert errors
- **Location** - `src/spread_arb/scanner.py:543-546`, `src/spread_arb/scanner.py:604-625`
- **What** - `_collect_spread_snapshots()` updates baselines before calling `insert_spread_snapshots()`. If the DB write fails, the in-memory engine still advances while the persisted history does not.
- **Why it matters** - A restart after snapshot-write failures will rebuild from older DB state than the live process used, changing signal readiness and thresholds unexpectedly.
- **Suggested action** - Only advance persistent-dependent baseline state after successful snapshot persistence, or explicitly separate the two histories.

## Low

### 13. Summary metrics undercount timeout-forced losers
- **Title** - `timeout_loss` trades are omitted from the close-reason summary line
- **Location** - `src/spread_arb/mean_reversion_engine.py:424-430`
- **What** - `log_summary()` includes `timeout=` but not `timeout_loss=`, even though `check_exits()` emits that distinct reason.
- **Why it matters** - Operations can misread close-reason distribution and think timeout behavior is healthier than it is.
- **Suggested action** - Include `timeout_loss` in the summary breakdown.

### 14. The execution test script repeats the min-qty/step-size bug
- **Title** - `scripts/test_execution.py` rounds by minimum quantity instead of lot increment
- **Location** - `scripts/test_execution.py:137-141`
- **What** - The standalone execution test uses `get_min_order_qty()` as the rounding increment in the same way as the main executor.
- **Why it matters** - A script intended to validate live execution can give false confidence or fail for the wrong reason on venues where `minQty != stepSize`.
- **Suggested action** - Reuse the fixed executor-side quantity-normalization logic once step size is separated from minimum size.

## Observations (not bugs)
- Snapshot collection and mean-reversion signal evaluation are effectively tied to the hardcoded 10-second snapshot loop in `src/spread_arb/scanner.py:519`, not to every incoming quote.
- The dynamic symbol rotator only discovers candidates from Binance, Bybit, Bitget, and Gate in `src/spread_arb/symbol_rotator.py:220-277`; OKX, MEXC, and HTX do not influence candidate discovery.
- The default `Settings.symbols` list in `src/spread_arb/config.py:31-43` is much larger than the 12-symbol production universe described in the request, so cold-start and preload behavior depends heavily on the actual `.env`.

## Files audited
- `src/spread_arb/main.py`
- `src/spread_arb/config.py`
- `src/spread_arb/scanner.py`
- `src/spread_arb/mean_reversion_engine.py`
- `src/spread_arb/execution.py`
- `src/spread_arb/storage.py`
- `src/spread_arb/models.py`
- `src/spread_arb/opportunity.py`
- `src/spread_arb/paper_engine.py`
- `src/spread_arb/symbol_rotator.py`
- `src/spread_arb/logging_setup.py`
- `src/spread_arb/exchanges/base.py`
- `src/spread_arb/exchanges/binance.py`
- `src/spread_arb/exchanges/bybit.py`
- `src/spread_arb/exchanges/bitget.py`
- `src/spread_arb/exchanges/gate.py`
- `src/spread_arb/exchanges/okx.py`
- `src/spread_arb/exchanges/htx.py`
- `src/spread_arb/exchanges/mexc.py`
- `src/spread_arb/ws_feeds/base.py`
- `src/spread_arb/ws_feeds/binance_ws.py`
- `src/spread_arb/ws_feeds/bybit_ws.py`
- `src/spread_arb/ws_feeds/bitget_ws.py`
- `src/spread_arb/ws_feeds/gate_ws.py`
- `src/spread_arb/ws_feeds/okx_ws.py`
- `src/spread_arb/ws_feeds/htx_ws.py`
- `scripts/test_execution.py`
- `scripts/backtest.py`

## Files skipped
- `src/spread_arb/__init__.py`, `src/spread_arb/exchanges/__init__.py`, `src/spread_arb/ws_feeds/__init__.py` - package glue only
- `src/spread_arb/exchanges/signing.py` - cryptographic helper functions, not part of strategy/execution correctness
- `src/spread_arb/**/__pycache__/*` - generated bytecode
- Remaining `scripts/` files - out of primary scope and not on the live execution path
