# Task: Audit the entire spread data pipeline for correctness and bias

## Goal

We are building a mean-reversion spread trading bot for crypto perpetual futures across 6 exchanges (Binance, Bybit, OKX, Bitget, Gate, HTX). We've collected ~10 hours of spread snapshot data and the analysis shows very promising results — possibly TOO promising. Before we invest time building trading logic, we need an independent audit of the entire data pipeline to make sure our numbers are real and not inflated by bugs, data artifacts, or flawed methodology.

The analysis currently shows:
- 58% of 2σ+ signals are profitable after 0.30% fees
- Top pairs show net spreads of +1.0% to +1.8% at 2σ threshold
- Estimated $5,000-12,000/day profit at $1,000 notional (we know this is theoretical max, but even 5-10% of this seems too good)

We need you to find any errors, biases, or methodological flaws that could be inflating these numbers.

## Files to audit

### 1. `src/spread_arb/scanner.py` — Data collection (method `_collect_spread_snapshots`, lines ~393-504)

**What it does:** Every 10 seconds, snapshots all current quotes, generates all exchange-pair combinations per symbol, computes spreads, writes to SQLite.

**Things to check:**
- Is the spread calculation correct? `spread_ab = (bid_b - ask_a) / ask_a * 100.0` — this represents "buy on A at ask, sell on B at bid". Is this the right formula for the raw spread of a long-A/short-B trade?
- The `best_raw_spread_pct = max(spread_ab, spread_ba)` — we take the MAX of both directions. **Is this creating a positive bias?** By always picking the better direction, we might be cherry-picking random noise. If both directions are normally distributed around a slightly negative mean, taking the max will systematically inflate the result.
- Exchange ordering normalization: when `ex_a.value > ex_b.value`, we swap everything. Is this swap done correctly? Do all the variables (bid, ask, spread, age) get swapped in the right way?
- Quote age filtering: quotes older than 30 seconds are excluded. Is 30 seconds too generous? Could stale quotes create artificial spread spikes?
- Are we correctly using `dict(self.latest_quotes)` to snapshot? Could there be race conditions with the async WS callbacks that corrupt data?

### 2. `src/spread_arb/storage.py` — Data storage (class `SpreadSnapshotRecord`, method `insert_spread_snapshots`)

**What it does:** Batch-inserts spread snapshot records into SQLite.

**Things to check:**
- Is the data written correctly? Column order matches the INSERT statement?
- Are there any type conversion issues (Decimal → float)?
- Could there be duplicate inserts (same timestamp + pair written twice)?

### 3. `scripts/analyze_spreads.py` — Analysis (functions `compute_stats` and `print_report`)

**What it does:** Loads all snapshots, groups by (symbol, exchange_a, exchange_b), computes statistical metrics per group, ranks by signal frequency and profitability.

**Critical things to check:**

#### Spread distribution analysis
- Population std dev (`/ n`) is used instead of sample std dev (`/ (n-1)`). With n≈3800 this barely matters, but flag it.
- The 2σ threshold is `mean + 2 * std`. Since `best_raw_spread_pct` is `max(spread_ab, spread_ba)`, the distribution is NOT normal — it's the max of two correlated values. **Does using σ thresholds on a non-normal distribution inflate signal count?**
- Are the percentile calculations correct? `sorted_spreads[int(n * 0.95)]` — is this off-by-one?

#### Profitability calculation
- `mean_net_spread_at_2sigma = mean(spreads where spread > 2σ threshold) - roundtrip_cost_pct`
- The roundtrip cost is 0.30% which represents `2 × taker_fee_exchange_A + 2 × taker_fee_exchange_B + slippage`. But different exchange pairs have different fee structures. **Using a flat 0.30% for all pairs is wrong** — some pairs (e.g., Binance+Bybit with 0.02%+0.055% taker fees) cost ~0.15% roundtrip, while others (e.g., HTX+Gate) cost more. This could make some pairs look profitable when they aren't, or hide profitable pairs.
- **Is the fee of 0.30% correct for a mean reversion trade?** In mean reversion, you enter AND exit the spread. That's 4 legs: open long on A, open short on B, close long on A, close short on B. So it's `2 × (fee_A + fee_B)` for a full roundtrip. The 0.30% default may or may not account for this correctly.

#### Est$/day calculation
```python
hours = s.count * 10 / 3600
signals_per_day = s.count_above_2sigma / hours * 24
est_daily = signals_per_day * s.mean_net_spread_at_2sigma / 100 * 1000
```
- This assumes every signal is tradeable and independent. In reality, signals on the same symbol across different pairs overlap (e.g., INJUSDT bybit→okx and INJUSDT bitget→okx fire at the same time because OKX moved). **The total profitable signal count is heavily double-counted.**
- The formula uses the pair's own snapshot count to estimate hours. Is this correct when different pairs might have different N values?

#### Mean reversion assumption
- The analysis identifies WHEN spreads exceed 2σ but **never validates that they actually revert**. It assumes that if you enter at 2σ+ and the mean spread is X, your profit is (entry_spread - mean - fees). But what if some excursions DON'T revert? What if the spread widens further?
- `avg_excursion_duration_snapshots` counts consecutive snapshots above 2σ, but this measures how long the spread STAYS elevated — not how quickly it returns to mean after dropping below 2σ. These are different things.

### 4. Cross-cutting concerns

- **Look-ahead bias**: The mean and std are computed over ALL data including the signals themselves. In real trading, you'd only know the mean/std from PAST data. This inflates the accuracy of signal detection. A proper test would use a rolling window or train/test split.
- **Survivorship bias in pairs**: We only analyze pairs where both exchanges had quotes. If an exchange went down temporarily, that gap isn't counted — but in real trading it would mean missed signals.
- **The `best_raw_spread_pct = max(ab, ba)` issue**: This deserves deep investigation. For each pair snapshot, we record the max of two directions. When we then compute mean and std of these max values, and flag when they exceed mean + 2σ, we're applying a statistical threshold to a distribution that has already been positively biased by the max() operation. **Please quantify this bias** — ideally by also computing stats on `spread_ab` and `spread_ba` separately and comparing.

## Deliverables

1. A written report (as comments in the code or a separate .md file) listing every issue found, categorized as:
   - **CRITICAL**: Would materially change profitability conclusions (e.g., 2x+ overestimate)
   - **MODERATE**: Introduces some bias but doesn't invalidate results (e.g., 10-30% overestimate)
   - **MINOR**: Technically wrong but negligible impact
   
2. For each CRITICAL issue: a proposed fix (code patch or pseudocode)

3. If possible: re-run the analysis with fixes applied and compare results to the current output to quantify the impact of each bias

## Context: current analysis output for reference

```
Total snapshots: 1,856,203 | Unique symbols: 50 | Unique pairs: 618
Top profitable pair: IMXUSDT bybit->okx, NetSpread +1.82%, 293 signals, Est $12,296/day
Overall: 86,445 total 2σ+ signals, 49,952 profitable (58%)
Quote ages: OKX avg 1816ms, Binance 2312ms, Bybit 1354-1660ms, Gate 2328-2556ms, HTX 3543-3713ms, Bitget 365-435ms
```

## What NOT to change

- Do NOT modify the data collection logic in scanner.py unless you find an actual bug
- Do NOT delete or modify the existing database
- Focus on ANALYSIS correctness, not code style or refactoring
- The snapshot collector itself works correctly (verified: consistent 10-second intervals, ~3800 snapshots per pair over ~10 hours)
