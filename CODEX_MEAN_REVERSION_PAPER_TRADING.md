# Task: Implement mean reversion paper trading engine

## Background

We have a crypto spread scanner that monitors perpetual futures across 6 exchanges (Binance, Bybit, OKX, Bitget, Gate, HTX) via WebSocket. It tracks bid/ask prices for 50 symbols and computes spreads between all exchange pairs.

We collected ~12 hours of spread snapshot data and ran a statistical audit. The audit confirmed that there IS a real statistical edge in mean reversion: spreads between certain exchange pairs deviate from their rolling mean and revert back. Top pairs show +0.4% to +1.2% net edge after fees.

**Key audit findings that MUST inform the design:**
- OKX is on one side of almost every profitable pair (both directions: X→OKX and OKX→X)
- 39% of snapshots have quotes older than 2 seconds — live trading must enforce strict freshness
- Signal overlap: same symbol fires on ~2.3 pairs simultaneously — must deduplicate at symbol level
- Rolling window baseline (not full-history) must be used for mean/std calculation
- Directional analysis (not `max(ab, ba)`) must be used for signals

## What already exists

### Current paper engine: `src/spread_arb/paper_engine.py`
The existing `PaperEngine` implements **instant arbitrage** — enter when raw spread exceeds a fixed threshold, exit when spread converges. This is NOT what we need. The mean reversion engine needs:
- A rolling statistical baseline (mean + std) per directional pair
- Entry when spread exceeds `mean + N*sigma` (using ONLY past data, no look-ahead)
- Exit when spread reverts to the rolling mean
- Stop-loss when spread widens further beyond a stop threshold

### Current config: `src/spread_arb/config.py`
Has per-exchange taker fees, paper notional, max positions, quote age limits, etc.

### Current scanner: `src/spread_arb/scanner.py`
- `_on_quote()` receives every WS quote and stores in `self.latest_quotes`
- `_collect_spread_snapshots()` runs every 10 seconds and writes to `spread_snapshots` table
- The scanner already calls `self.paper_engine.on_quote_tick()` on every quote
- The scanner already calls `self.paper_engine.on_observed_opportunity()` for opportunities

### Current storage: `src/spread_arb/storage.py`
Has `PaperTradeRecord`, `OpportunityStore.insert_paper_trade()`, `paper_trades` table.

## What to build

Create a new `MeanReversionEngine` class (in a new file `src/spread_arb/mean_reversion_engine.py`) that replaces the role of `PaperEngine` for mean reversion paper trading. DO NOT modify the existing `PaperEngine` — create a new file.

### Core data structures

```python
@dataclass
class RollingBaseline:
    """Rolling mean and std for a specific directional pair."""
    window: deque[float]  # last N spread values
    window_size: int      # configurable, default 360 (= 1 hour at 10s intervals)
    sum_x: float
    sum_x2: float
    
    @property
    def mean(self) -> float: ...
    
    @property 
    def std(self) -> float: ...
    
    @property
    def is_ready(self) -> bool:
        """Need at least window_size samples before generating signals."""
        return len(self.window) >= self.window_size
    
    def update(self, value: float) -> None:
        """Add new spread value, remove oldest if window is full."""
        ...

@dataclass
class MeanRevPosition:
    """An open mean reversion paper position."""
    symbol: str
    long_exchange: ExchangeName
    short_exchange: ExchangeName
    direction: str               # "ab" or "ba" — which directional spread triggered entry
    notional_usdt: float
    opened_at: datetime
    entry_long_price: float
    entry_short_price: float
    entry_spread_pct: float      # the directional spread at entry (NOT max)
    entry_rolling_mean: float    # rolling mean at time of entry
    entry_rolling_std: float     # rolling std at time of entry
    sigma_at_entry: float        # how many sigmas above mean at entry
    max_adverse_spread_pct: float
    max_favorable_spread_pct: float
```

### Integration with scanner

The engine needs spread data from TWO sources:

1. **High-frequency quote updates** — from `_on_quote()`, to check open position exits. The engine needs the DIRECTIONAL spread (not `max(ab, ba)`), so it must compute spreads itself from raw quotes.

2. **Periodic baseline updates** — from `_collect_spread_snapshots()` every 10 seconds, to update rolling baselines and check for entry signals.

Modify `scanner.py` to:
- Create `MeanReversionEngine` alongside (or instead of) `PaperEngine`
- Call `engine.update_baselines(snapshot_quotes)` from `_collect_spread_snapshots()` every 10 seconds — this updates rolling baselines AND checks for new entry signals
- Call `engine.check_exits(latest_quotes)` from `_on_quote()` — this checks if any open position should be closed

### Entry logic

Called every 10 seconds from `_collect_spread_snapshots`:

```
For each symbol with quotes from 2+ exchanges:
    For each ordered pair (long_exchange, short_exchange):
        Compute directional spread: (bid_short - ask_long) / ask_long * 100
        Update RollingBaseline for this (symbol, long_ex, short_ex) triple
        
        If baseline is not ready (< window_size samples): skip
        If quote age > max_quote_age_ms (use 2000ms, not 30000ms!): skip
        
        threshold = baseline.mean + sigma_multiplier * baseline.std
        
        If spread > threshold:
            This is a SIGNAL. Check tradeability:
            - No existing position on this symbol (one_position_per_symbol)
            - Max open positions not reached
            - Symbol cooldown not active  
            - Both quotes are fresh (< 2s old)
            - Liquidity check: best_ask_size / best_bid_size covers notional
            
            Compute roundtrip cost using PER-EXCHANGE fees:
                cost = 2 * (fee_long + fee_short) + slippage_buffer + safety_buffer
            
            expected_edge = spread - baseline.mean
            net_edge = expected_edge - cost
            
            If net_edge > 0: OPEN POSITION
                - Record entry prices, rolling mean/std at entry
                - Simulate execution delay (500ms default)
                - After delay, re-check quotes and spread (it may have reverted)
```

### Exit logic

Called on every quote update from `_on_quote`:

```
For each open position:
    Get current quotes for long_exchange and short_exchange
    If quotes missing or stale: close with reason "stale_quote"
    
    Compute current directional spread (same direction as entry)
    Update max_adverse and max_favorable
    
    EXIT CONDITIONS (check in order):
    1. Spread reverted to mean: current_spread <= position.entry_rolling_mean
       → close with reason "mean_reversion" (this is the desired outcome)
    
    2. Spread widened beyond stop: 
       current_spread > entry_rolling_mean + stop_sigma * entry_rolling_std
       → close with reason "stop_loss"
       (stop_sigma should be configurable, default 4.0 — i.e., if we entered at 2σ and spread goes to 4σ, cut losses)
    
    3. Max hold time exceeded: hold_seconds > max_hold_seconds (default 900 = 15 min)
       → close with reason "timeout"
    
    4. Quotes became stale: age > max_quote_age_ms
       → close with reason "stale_quote"
```

### PnL calculation

Use the existing `calculate_pnl()` function from `paper_engine.py` — it correctly handles:
- Long PnL: `notional * (exit_long_price - entry_long_price) / entry_long_price`
- Short PnL: `notional * (entry_short_price - exit_short_price) / entry_short_price`
- Fee deduction, slippage deduction
- Net PnL as percentage of total exposure (2 × notional)

Import and reuse it, don't rewrite.

### Recording trades

Use the existing `PaperTradeRecord` and `OpportunityStore.insert_paper_trade()`. The `close_reason` field should distinguish mean reversion outcomes: "mean_reversion", "stop_loss", "timeout", "stale_quote".

### Config additions

Add these to `Settings` in `config.py`:

```python
# Mean reversion settings
mr_enabled: bool = True
mr_sigma_entry: float = Field(default=2.0, gt=0)       # entry when spread > mean + N*sigma
mr_sigma_stop: float = Field(default=4.0, gt=0)        # stop loss at mean + N*sigma  
mr_rolling_window: int = Field(default=360, ge=30)      # rolling window size in snapshots (360 = 1 hour)
mr_min_net_edge_pct: float = Field(default=0.10, ge=0)  # minimum net edge after fees to enter
mr_max_positions: int = Field(default=1, ge=1)          # max simultaneous MR positions
mr_notional_usdt: float = Field(default=350.0, gt=0)    # notional per leg
mr_max_hold_seconds: int = Field(default=900, ge=1)     # max hold time (15 min default)
mr_cooldown_sec: int = Field(default=30, ge=0)          # cooldown after closing a symbol
```

### Logging

The engine must log extensively for diagnostics:

```
# On baseline becoming ready:
INFO  mr baseline ready | INJUSDT okx->bybit | window=360 | mean=-0.0312% | std=0.4521%

# On signal detection:
INFO  mr signal | INJUSDT okx->bybit | spread=0.9823% | mean=-0.0312% | std=0.4521% | sigma=2.24 | net_edge=+0.7135%

# On position open:
INFO  mr open | INJUSDT | long=okx @ 25.432 | short=bybit @ 25.465 | spread=0.9823% | mean=-0.0312% | sigma=2.24

# On position close:
INFO  mr close | INJUSDT | reason=mean_reversion | hold=184s | net_pnl=+0.42 USDT (+0.06%) | entry_spread=0.98% | exit_spread=-0.03%

# Periodic summary (every 60 seconds):
INFO  mr summary | open=1 | closed=47 | wins=31 (65.9%) | total_pnl=+12.84 USDT | avg_hold=156s | avg_pnl=+0.27 USDT
```

### Summary loop

Every 60 seconds, log aggregate stats:
- Open positions count
- Total closed trades
- Win rate (closed with reason "mean_reversion" and positive PnL)
- Total net PnL
- Average hold time
- Breakdown by close reason (mean_reversion / stop_loss / timeout / stale_quote)
- Top 5 symbols by PnL contribution

## Important constraints

1. **DO NOT use `max(spread_ab, spread_ba)`** for signals. Always use directional spreads. Each direction is an independent trading signal with its own rolling baseline.

2. **Rolling window must use ONLY past data.** At time t, mean and std are computed from samples [t-window, t-1]. The current value at time t is NOT included in the baseline used to evaluate it.

3. **Quote freshness for entry signals: use 2000ms** (the live trading threshold), NOT the 30000ms used in snapshot collection. The snapshot collector intentionally uses a wider window to capture the full picture, but the trading engine must be strict.

4. **Per-exchange fees.** Use `Settings.taker_fee_*_pct` for each exchange in the pair, not a flat 0.30%.

5. **One position per symbol.** If INJUSDT is open on okx→bybit, don't open INJUSDT on okx→bitget even if it signals.

6. **The engine runs IN the main event loop** (same as the current PaperEngine). Don't create separate threads. The snapshot starvation issue has been fixed by Codex already.

7. **Reuse existing infrastructure:** `calculate_pnl()`, `PaperTradeRecord`, `OpportunityStore`, `decide_close_reason()` where applicable. Import from `paper_engine.py`.

8. **The exchange_fees_pct dict must include ALL exchanges**, not just MEXC and Bybit like the current PaperEngine (line 137-140 of paper_engine.py — this was a bug in the old engine).

## Files to create/modify

- **CREATE:** `src/spread_arb/mean_reversion_engine.py` — the new engine
- **MODIFY:** `src/spread_arb/config.py` — add `mr_*` settings
- **MODIFY:** `src/spread_arb/scanner.py` — integrate `MeanReversionEngine`:
  - Create the engine in `run()`
  - Call `engine.update_baselines()` from `_collect_spread_snapshots()`
  - Call `engine.check_exits()` from `_on_quote()`
  - Add engine summary to the shutdown flow
- **DO NOT MODIFY:** `src/spread_arb/paper_engine.py` — keep the old engine as-is
- **DO NOT MODIFY:** `src/spread_arb/storage.py` — reuse existing tables and methods

## How to verify

1. Run the bot for 10 minutes with `mr_enabled=True`
2. Check logs for "mr baseline ready" messages (should appear after ~60 minutes of data, or immediately if rolling window is reduced for testing)
3. Check logs for "mr signal", "mr open", "mr close" messages
4. Query the database: `SELECT close_reason, COUNT(*), ROUND(AVG(net_pnl_usdt), 4) FROM paper_trades GROUP BY close_reason;`
5. Verify win rate and PnL distribution make sense

## Testing shortcut

For faster testing, reduce `mr_rolling_window` to 30 (5 minutes of data) so the engine starts generating signals sooner. Remember to set it back to 360 for production data collection.
