# Task: Add BBO width filter to discover_symbols.py

## Problem

`scripts/discover_symbols.py` currently ranks symbols by cross-exchange spread but doesn't check the bid-ask width on individual exchanges. Symbols with wide BBO (thin orderbooks) show up as high-spread candidates, but the "spread" is fake — it's just the bid-ask gap, not a real arbitrage opportunity.

Example: ALCHUSDT showed 0.29% cross-exchange spread, but Gate's BBO was 25-35 bps. The bot entered and lost $0.07.

## Solution

Filter out symbols where any exchange has BBO > a threshold (default 15 bps), and display BBO info in the output for transparency.

## File to modify

`scripts/discover_symbols.py`

## Changes needed

### 1. Add `--max-bbo` CLI argument

Add to the argument parser:
```python
parser.add_argument("--max-bbo", type=float, default=15.0, help="Max BBO spread in bps per exchange (default: 15)")
```

### 2. Track BBO width in quote fetchers

Each quote fetcher (`fetch_binance_quotes`, `fetch_bybit_quotes`, `fetch_bitget_quotes`, `fetch_gate_quotes`) already returns `dict[str, tuple[float, float]]` where the tuple is `(bid, ask)`.

No change needed in the fetchers — the BBO can be calculated from `(ask - bid) / bid * 10000` downstream.

### 3. Add BBO check in `calc_spreads()`

Update `calc_spreads()` to:
- Accept the raw quotes dict (currently it receives `dict[str, tuple[float, float]]` keyed by exchange name)
- Calculate BBO in bps for each exchange
- Track `max_bbo_bps` (worst/widest BBO across exchanges for this symbol)
- Add `max_bbo_bps` to the `SymbolSpread` dataclass

Update the `SymbolSpread` dataclass:
```python
@dataclass
class SymbolSpread:
    symbol: str
    max_spread_pct: float
    best_pair: str
    spreads: dict[str, float]
    num_exchanges: int
    is_new: bool
    max_bbo_bps: float  # NEW: widest BBO across exchanges
```

In `calc_spreads()`, compute BBO for each exchange:
```python
max_bbo = 0.0
for ex_name, (bid, ask) in quotes.items():
    bbo_bps = (ask - bid) / bid * 10_000 if bid > 0 else 0
    max_bbo = max(max_bbo, bbo_bps)
```

### 4. Filter by max BBO in `run()`

After calculating spreads, filter out symbols where `max_bbo_bps > max_bbo_arg`:

```python
if max_bbo > 0 and spread.max_bbo_bps > max_bbo:
    continue  # skip thin orderbook symbols
```

### 5. Display BBO in output

Update the results table header and rows to include BBO:

```
  # Symbol              Spread% Direction            Exch BBO_bps Status
```

Show `max_bbo_bps` rounded to 1 decimal place in each row.

### 6. Add a summary line

After the results table, show how many symbols were filtered out by BBO:

```python
print(f"Filtered by BBO (>{max_bbo} bps): {bbo_filtered_count}")
```

## Do NOT

- Do not modify the quote fetcher functions — BBO is computed from existing bid/ask data
- Do not change the `CURRENT_SYMBOLS` set
- Do not change the `normalize_symbol()` function
- Do not modify any files other than `scripts/discover_symbols.py`
