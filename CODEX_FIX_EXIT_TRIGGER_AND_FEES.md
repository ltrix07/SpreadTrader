# Task: Fix exit trigger spread calculation + fail-closed fee lookup

## Context

The MeanReversionEngine (`src/spread_arb/mean_reversion_engine.py`) has two issues identified during code review.

## Fix 1: Exit trigger uses "entry-style" spread instead of "unwind" spread

### Problem

In `check_exits()`, the current spread used to decide whether to close is calculated as:

```python
current_spread_pct = _directional_spread_pct(
    ask_long=long_quote.best_ask_price,
    bid_short=short_quote.best_bid_price,
)
```

This is the "entry-style" spread — the spread you'd get if entering a NEW position (buy at ask, sell at bid).

But when CLOSING an existing position, the actual execution is:
- Sell long at `bid_long` (not ask_long)
- Buy back short at `ask_short` (not bid_short)

And the PnL calculation in `_close_position()` correctly uses these prices:
```python
exit_long_price = float(long_quote.best_bid_price)
exit_short_price = float(short_quote.best_ask_price)
```

So the exit TRIGGER checks the wrong spread, while the exit EXECUTION uses the right prices. This means:
- The bot may close thinking the spread reached the target, but actual PnL is worse by the sum of bid-ask spreads on both exchanges (~0.02-0.04%)
- Or the bot may hold longer than needed when actual PnL is already good but "entry-style" spread hasn't reached target yet

### Fix

Add a PnL floor check to the mean_reversion exit condition. Don't close with reason "mean_reversion" if the estimated net PnL at current close prices would be negative.

In `check_exits()`, after determining `close_reason = "mean_reversion"`, compute the estimated PnL:

```python
if current_spread_pct <= position.take_profit_target:
    # Verify that closing at actual bid/ask prices would be profitable
    est_exit_long = float(long_quote.best_bid_price)
    est_exit_short = float(short_quote.best_ask_price)
    
    exit_fees = _one_side_fees_usdt(
        notional_usdt=position.notional_usdt,
        fee_long_pct=self.exchange_fees_pct.get(position.long_exchange, 0.0),
        fee_short_pct=self.exchange_fees_pct.get(position.short_exchange, 0.0),
    )
    exit_slippage = _one_side_slippage_usdt(
        notional_usdt=position.notional_usdt,
        slippage_buffer_pct=self.settings.slippage_buffer_pct,
    )
    
    est_pnl = calculate_pnl(
        notional_usdt=position.notional_usdt,
        entry_long_price=position.entry_long_price,
        entry_short_price=position.entry_short_price,
        exit_long_price=est_exit_long,
        exit_short_price=est_exit_short,
        entry_fees_usdt=position.estimated_entry_fees_usdt,
        exit_fees_usdt=exit_fees,
        entry_slippage_usdt=position.estimated_entry_slippage_usdt,
        exit_slippage_usdt=exit_slippage,
    )
    
    if est_pnl.net_pnl_usdt > 0:
        close_reason = "mean_reversion"
    else:
        # Spread reached target but actual PnL is negative — keep holding
        close_reason = None
```

Also log when this guard triggers so we can monitor it:
```python
    else:
        self.log.debug(
            "mr exit guard | %s | spread reached target but est_pnl=%+.2f — holding",
            symbol, est_pnl.net_pnl_usdt,
        )
        close_reason = None
```

**Important:** This PnL guard ONLY applies to `mean_reversion` closes. Stop-loss, timeout, and stale_quote exits must proceed regardless of PnL — they are risk management exits.

### Files
- `src/spread_arb/mean_reversion_engine.py` — modify `check_exits()`

## Fix 2: Fail-closed fee lookup

### Problem

In `_evaluate_signal()` and `_close_position()`, exchange fees are looked up with:

```python
self.exchange_fees_pct.get(long_exchange, 0.0)
```

If an exchange is missing from the fee map (e.g., new exchange added but fee not configured), it defaults to `0.0` — meaning "free trading". This is a fail-open design. The bot would overestimate profitability and enter bad trades.

### Fix

Replace the 0.0 default with a safe fallback fee of 0.10% (double the typical taker fee). This ensures unknown exchanges are penalized rather than subsidized.

**In `__init__`**, add a class constant:
```python
_FALLBACK_FEE_PCT = 0.10  # Conservative fallback for unknown exchanges
```

**Then replace ALL instances of:**
```python
self.exchange_fees_pct.get(exchange, 0.0)
```

**With:**
```python
self.exchange_fees_pct.get(exchange, self._FALLBACK_FEE_PCT)
```

And add a warning log when the fallback is used. Add a helper method:

```python
def _get_fee_pct(self, exchange: ExchangeName) -> float:
    fee = self.exchange_fees_pct.get(exchange)
    if fee is None:
        self.log.warning("no fee configured for %s, using fallback %.2f%%", exchange.value, self._FALLBACK_FEE_PCT)
        return self._FALLBACK_FEE_PCT
    return fee
```

Then replace all `.get(exchange, 0.0)` calls with `self._get_fee_pct(exchange)`.

Instances to replace (search for `exchange_fees_pct.get`):
1. `_evaluate_signal()` — roundtrip cost calculation
2. `_execute_after_delay()` — roundtrip cost + entry fees
3. `_close_position()` — exit fees

### Files
- `src/spread_arb/mean_reversion_engine.py` — add `_get_fee_pct()`, replace all `.get(..., 0.0)` calls

## Summary of all changes

| File | Change |
|------|--------|
| `src/spread_arb/mean_reversion_engine.py` | Add PnL floor guard in `check_exits()` for mean_reversion closes. Add `_get_fee_pct()` helper with fallback + warning. Replace all `exchange_fees_pct.get(x, 0.0)` with `_get_fee_pct(x)`. |

## How to verify

1. **PnL guard**: After deploying, check logs for `mr exit guard` messages — these show cases where the bot would have closed at a loss but now holds instead.
2. **Fee fallback**: Temporarily remove one exchange from the fee map and verify the warning appears in logs.
3. Run for 2+ hours with paper trading and verify:
   - `mean_reversion` closes all have positive net_pnl in the database
   - No `mean_reversion` close has negative net_pnl (query: `SELECT * FROM paper_trades WHERE close_reason='mean_reversion' AND net_pnl_usdt < 0`)
