# Task: Add BBO width filter to MR engine

## Problem

The bot enters trades on symbols with thin orderbooks (e.g. ALCHUSDT with 25-35 bps BBO spread on Gate). The cross-exchange "spread" is partly just bid-ask width on individual exchanges — not a real arbitrage opportunity. On execution, the fill is worse than BBO, and the trade loses money.

Example: ALCHUSDT bybit->bitget entered at 0.83% cross-exchange spread, but individual BBO widths were 10-35 bps per exchange. The trade lost $0.07 despite "mean_reversion" exit.

## Solution

Add a configurable BBO width filter that rejects MR signals where either exchange's bid-ask spread exceeds a threshold.

## Files to modify

### 1. `src/spread_arb/config.py`

Add after `mr_min_quote_freshness_pct` (line 85):

```python
mr_max_bbo_spread_bps: float = Field(default=15.0, ge=0)
```

- Default 15 bps (0.15%). Symbols with BBO wider than this on either exchange are rejected.
- Setting to 0 disables the filter.

### 2. `src/spread_arb/mean_reversion_engine.py`

In `_evaluate_signal()` method, add a BBO width check **after** the freshness filter and **before** the baseline/sigma calculations (i.e., before `key = (symbol, long_exchange, short_exchange)`). This ensures we reject early, before doing expensive computations.

Logic:
```python
max_bbo_bps = self.settings.mr_max_bbo_spread_bps
if max_bbo_bps > 0:
    long_bbo_bps = float(
        (long_quote.best_ask_price - long_quote.best_bid_price)
        / long_quote.best_bid_price
    ) * 10_000
    short_bbo_bps = float(
        (short_quote.best_ask_price - short_quote.best_bid_price)
        / short_quote.best_bid_price
    ) * 10_000
    if long_bbo_bps > max_bbo_bps or short_bbo_bps > max_bbo_bps:
        return
```

Both `long_quote` and `short_quote` are `Quote` objects (from `models.py`) with `best_bid_price` and `best_ask_price` as `Decimal` fields. The calculation converts to bps (basis points, 1 bps = 0.01%).

### 3. Also add BBO info to the signal log line

Update the `mr signal` log line in `_evaluate_signal()` to include BBO widths for debugging. The log line is around line 582. Add `bbo=X/Y` showing long and short BBO in bps:

Current:
```python
self.log.info(
    "mr signal | %s %s->%s | spread=%+.4f%% | mean=%+.4f%% | std=%.4f%% | sigma=%.2f | net_edge=%+.4f%% | fresh=%.0f%%/%.0f%%",
    ...
)
```

New:
```python
self.log.info(
    "mr signal | %s %s->%s | spread=%+.4f%% | mean=%+.4f%% | std=%.4f%% | sigma=%.2f | net_edge=%+.4f%% | fresh=%.0f%%/%.0f%% | bbo=%.1f/%.1f",
    symbol,
    long_exchange.value,
    short_exchange.value,
    spread_pct,
    mean,
    std,
    sigma,
    net_edge_pct,
    long_fresh_pct,
    short_fresh_pct,
    long_bbo_bps,
    short_bbo_bps,
)
```

For this to work, `long_bbo_bps` and `short_bbo_bps` must be computed BEFORE the log line. Move the BBO calculation before the baseline check so it's always available, or compute it twice (once for filter, once for logging). Simplest: compute BBO at the top of the method and use it in both places.

## Important

- The BBO filter should only apply to signal evaluation, NOT to exit checks. We don't want to hold a position longer because BBO widened.
- Do NOT modify any other filters or thresholds.
- Do NOT change the baseline update logic.
- Do NOT modify `check_exits()` or `_close_position()`.
- The `Quote` model already has `best_bid_price` and `best_ask_price` — no changes needed in `models.py`.

## Testing

After implementation, the bot should:
1. Accept signals where both exchanges have BBO < 15 bps
2. Silently reject signals where either exchange BBO > 15 bps  
3. Log BBO values in the `mr signal` line for all signals that pass other pre-checks
