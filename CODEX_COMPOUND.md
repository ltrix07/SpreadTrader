# Task: Add Auto-Compounding (Dynamic Notional Sizing)

## Overview

Currently the bot uses a fixed `MR_NOTIONAL_USDT` for every trade. Add compound mode: the bot calculates notional dynamically as a percentage of available balance, so position size grows with profits.

## Config Changes — `src/spread_arb/config.py`

Add these fields to `Settings`:

```python
    mr_compound_enabled: bool = True
    mr_notional_pct: float = Field(default=85.0, gt=0, le=100)  # % of available balance per leg
    mr_min_notional_usdt: float = Field(default=5.0, ge=1)  # floor — don't trade below this
```

When `mr_compound_enabled=True`, the bot ignores `mr_notional_usdt` and instead uses `mr_notional_pct` of the available balance. `mr_notional_usdt` becomes the fallback for paper trading mode (where there's no real balance to query).

## Execution Service Changes — `src/spread_arb/execution.py`

Add a method to `ExecutionService` that calculates dynamic notional:

```python
async def calculate_notional(
    self,
    long_exchange: ExchangeName,
    short_exchange: ExchangeName,
) -> float:
    """Calculate notional per leg based on available balance on both exchanges.
    
    Returns the notional in USDT, or 0.0 if balance is insufficient.
    """
    if not self.settings.mr_compound_enabled:
        return self.settings.mr_notional_usdt

    try:
        long_balance, short_balance = await asyncio.gather(
            self.clients[long_exchange].get_balance(),
            self.clients[short_exchange].get_balance(),
        )
    except Exception as exc:
        self.log.warning("balance fetch failed for notional calc: %s — using fixed notional", exc)
        return self.settings.mr_notional_usdt

    # Use the smaller available balance as the constraint
    min_available = min(float(long_balance.available_usdt), float(short_balance.available_usdt))
    
    # Calculate notional as percentage of available balance
    notional = min_available * self.settings.mr_notional_pct / 100.0
    
    # Apply floor
    if notional < self.settings.mr_min_notional_usdt:
        self.log.warning(
            "compound notional $%.2f below minimum $%.2f — skipping trade",
            notional, self.settings.mr_min_notional_usdt,
        )
        return 0.0
    
    # Apply safety cap
    if notional > self.settings.max_notional_usdt:
        notional = self.settings.max_notional_usdt
    
    self.log.debug(
        "compound notional: $%.2f (%.0f%% of min balance $%.2f)",
        notional, self.settings.mr_notional_pct, min_available,
    )
    return notional
```

**Important:** Balance queries add latency. To avoid calling get_balance() on every signal evaluation, cache the balances with a TTL:

```python
def __init__(self, ...):
    ...
    self._balance_cache: dict[ExchangeName, tuple[float, float]] = {}  # exchange -> (available_usdt, timestamp)
    self._balance_cache_ttl: float = 30.0  # seconds

async def _get_cached_balance(self, exchange: ExchangeName) -> float:
    """Get available balance with 30s cache."""
    import time
    now = time.time()
    cached = self._balance_cache.get(exchange)
    if cached and (now - cached[1]) < self._balance_cache_ttl:
        return cached[0]
    
    balance = await self.clients[exchange].get_balance()
    available = float(balance.available_usdt)
    self._balance_cache[exchange] = (available, now)
    return available
```

Then use `_get_cached_balance()` inside `calculate_notional()` instead of calling `get_balance()` directly.

## Engine Changes — `src/spread_arb/mean_reversion_engine.py`

### In `_execute_after_delay()`

In the live mode branch, BEFORE placing orders, calculate dynamic notional:

```python
if self.live_mode:
    notional = await self.execution_service.calculate_notional(
        long_exchange=current.long_exchange,
        short_exchange=current.short_exchange,
    )
    if notional <= 0:
        self.log.info("mr skip | %s | insufficient balance for compound notional", symbol)
        return
else:
    notional = self.settings.mr_notional_usdt
```

Then use this `notional` variable everywhere in the method instead of `self.settings.mr_notional_usdt`. Specifically:
- Pass it to `execute_spread_entry(notional_usdt=notional, ...)`
- Use it for fee/slippage estimation
- Store it in the `MeanRevPosition` object

### In the signal evaluation logging

Log the dynamic notional so we can see what size the bot is using:

```python
self.log.info(
    "mr entry | %s | %s->%s | notional=$%.2f | ...",
    symbol, long_exchange, short_exchange, notional, ...
)
```

## Paper Trading Mode

When `mr_compound_enabled=True` but `live_mode=False` (paper trading), fall back to `mr_notional_usdt` since there's no real balance to query. Log a note at startup:

```python
if self.settings.mr_compound_enabled and not self.live_mode:
    self.log.info("compound enabled but running in paper mode — using fixed notional $%.2f", self.settings.mr_notional_usdt)
```

## Update `.env.example`

Add under Mean Reversion Strategy section:

```
MR_COMPOUND_ENABLED=true
MR_NOTIONAL_PCT=25
MR_MIN_NOTIONAL_USDT=5
```

Remove `MR_NOTIONAL_USDT=10` line (or keep it as fallback with a comment that it's only used in paper mode).

## Files to change

| File | Action | Description |
|------|--------|-------------|
| `src/spread_arb/config.py` | MODIFY | Add `mr_compound_enabled`, `mr_notional_pct`, `mr_min_notional_usdt` |
| `src/spread_arb/execution.py` | MODIFY | Add `calculate_notional()`, `_get_cached_balance()`, balance cache |
| `src/spread_arb/mean_reversion_engine.py` | MODIFY | Use dynamic notional in `_execute_after_delay()` live branch |
| `.env.example` | MODIFY | Add compound settings |

## Verification

1. Paper mode: `LIVE_TRADING=false` — bot should use `MR_NOTIONAL_USDT` as before, no behavior change
2. Live mode with compound: `LIVE_TRADING=true, MR_COMPOUND_ENABLED=true` — check logs for "compound notional: $X.XX" messages
3. Verify notional grows: after a few profitable trades, the logged notional should increase
4. Verify floor: if balance drops below `MR_MIN_NOTIONAL_USDT / MR_NOTIONAL_PCT * 100`, bot should skip trades
5. Verify cap: notional should never exceed `MAX_NOTIONAL_USDT`
