# Codex Task: Spread Lifecycle Collector

## Goal
Create a **standalone script** `scripts/spread_lifecycle.py` that connects to WebSocket BBO feeds on all 5 exchanges (binance, bybit, bitget, gate, okx), computes directional spreads for **all 10 exchange pairs** across all configured symbols (base SYMBOLS from `.env` + dynamic rotation symbols), and records the full lifecycle of each "spread event" into a dedicated SQLite database.

The script runs **in parallel** with the main trading bot (separate process, separate DB) for 24+ hours, collecting raw data for later statistical analysis. It does NOT trade — only observes and records.

## Architecture

### Reuse existing code
- **WS feeds**: Reuse `src/spread_arb/ws_feeds/` — `BinanceWsFeed`, `BybitWsFeed`, `BitgetWsFeed`, `GateWsFeed`, `OkxWsFeed`. All inherit from `WebSocketFeed` base class and produce `Quote` objects via `on_quote` callback.
- **Config**: Reuse `src/spread_arb/config.py` `Settings` to read `.env` for SYMBOLS, EXCHANGES, fee settings.
- **Models**: Reuse `src/spread_arb/models.py` for `Quote`, `ExchangeName`, `Symbol`.
- **Symbol rotator**: Reuse `src/spread_arb/symbol_rotator.py` `discover_candidates()` if `DYNAMIC_ROTATION_ENABLED=true`, to get the same dynamic symbols the bot uses.

### Spread event lifecycle tracking

A **spread event** is a continuous period where the directional raw spread for a specific `(symbol, exchange_pair, direction)` stays above a configurable threshold.

**Directional spread** = `(bid_short - ask_long) / ask_long * 100%` — same formula used in the main bot.

**Exchange pair** = ordered pair like `binance→bybit` meaning "buy on binance (ask), sell on bybit (bid)". For each symbol × each of the 10 exchange combinations × 2 directions = check all, but only track events where spread > threshold.

### Event lifecycle states

```
INACTIVE → ACTIVE (spread crosses above threshold)
ACTIVE → tracking samples every quote update
ACTIVE → CLOSED (spread drops below threshold for >= cooldown period, e.g. 3 seconds)
```

### Data to record per event

For each spread event, record these in the `spread_events` table:

```sql
CREATE TABLE spread_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    long_exchange TEXT NOT NULL,      -- exchange where we'd buy (ask side)
    short_exchange TEXT NOT NULL,     -- exchange where we'd sell (bid side)
    
    -- Timing
    started_at TEXT NOT NULL,         -- ISO timestamp when spread first crossed threshold
    peaked_at TEXT NOT NULL,          -- ISO timestamp of maximum spread
    ended_at TEXT NOT NULL,           -- ISO timestamp when spread dropped below threshold
    duration_ms INTEGER NOT NULL,     -- total event duration in milliseconds
    rise_duration_ms INTEGER NOT NULL,   -- time from start to peak
    fall_duration_ms INTEGER NOT NULL,   -- time from peak to end
    
    -- Spread values (all in percent)
    threshold_pct REAL NOT NULL,      -- the threshold used
    entry_spread_pct REAL NOT NULL,   -- spread at the moment event started
    peak_spread_pct REAL NOT NULL,    -- maximum spread during event
    exit_spread_pct REAL NOT NULL,    -- spread when event ended
    mean_spread_pct REAL NOT NULL,    -- average spread during event
    
    -- Velocity (percent per second)
    rise_velocity REAL NOT NULL,      -- (peak - entry) / rise_duration_sec
    fall_velocity REAL NOT NULL,      -- (peak - exit) / fall_duration_sec
    
    -- Shape metrics
    num_samples INTEGER NOT NULL,     -- number of quote updates during event
    time_above_peak50_ms INTEGER NOT NULL,  -- ms spent above 50% of (peak - threshold)
    time_above_peak75_ms INTEGER NOT NULL,  -- ms spent above 75% of (peak - threshold)
    spread_std REAL NOT NULL,         -- standard deviation of spread samples during event
    
    -- BBO context
    avg_bbo_long_bps REAL,           -- average bid-ask spread on the long exchange during event
    avg_bbo_short_bps REAL,          -- average bid-ask spread on the short exchange during event
    
    -- Simulated trade outcome
    -- If we entered at event start (entry_spread_pct) and exited at mean reversion (threshold),
    -- what would the PnL be after fees?
    simulated_gross_pnl_pct REAL NOT NULL,  -- entry_spread_pct - threshold_pct
    simulated_net_pnl_pct REAL NOT NULL,    -- gross minus roundtrip cost (2*fee_long + 2*fee_short + slippage)
    
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_spread_events_symbol ON spread_events(symbol);
CREATE INDEX idx_spread_events_pair ON spread_events(long_exchange, short_exchange);
CREATE INDEX idx_spread_events_peak ON spread_events(peak_spread_pct);
CREATE INDEX idx_spread_events_started ON spread_events(started_at);
```

Also record raw tick-level data for detailed analysis:

```sql
CREATE TABLE spread_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES spread_events(id),
    ts TEXT NOT NULL,               -- ISO timestamp
    spread_pct REAL NOT NULL,       -- current spread
    bid_short REAL NOT NULL,        -- bid price on short exchange
    ask_long REAL NOT NULL,         -- ask price on long exchange
    bbo_long_bps REAL,             -- BBO width on long exchange
    bbo_short_bps REAL,            -- BBO width on short exchange
    elapsed_ms INTEGER NOT NULL     -- ms since event start
);

CREATE INDEX idx_spread_ticks_event ON spread_ticks(event_id);
```

### Important: tick sampling rate

To avoid massive DB size, do NOT record every single WS message as a tick. Instead:
- Record a tick at most every **200ms** per active event (5 ticks/sec max).
- Always record the first tick (event start), the peak tick, and the last tick (event end).

## CLI interface

```bash
# Basic usage — reads SYMBOLS and EXCHANGES from .env
python -m scripts.spread_lifecycle --threshold 0.10

# Custom threshold, custom DB path
python -m scripts.spread_lifecycle --threshold 0.05 --db data/spread_lifecycle.sqlite3

# With dynamic symbols included
python -m scripts.spread_lifecycle --threshold 0.10 --include-dynamic

# Cooldown before closing event (spread must stay below threshold for N sec)
python -m scripts.spread_lifecycle --threshold 0.10 --cooldown 3.0

# Limit runtime
python -m scripts.spread_lifecycle --threshold 0.10 --max-hours 24
```

Default values:
- `--threshold`: 0.10 (percent)
- `--db`: `data/spread_lifecycle.sqlite3`
- `--include-dynamic`: flag, off by default
- `--cooldown`: 3.0 (seconds)
- `--max-hours`: 0 (unlimited)

## Implementation details

### Quote storage & spread calculation

Maintain an in-memory dict of latest quotes per `(exchange, symbol)`. On each new quote:
1. Update the quote in the dict
2. For this symbol, recalculate spreads for all exchange pairs where we have fresh quotes on both sides (age < 5 seconds)
3. For each `(symbol, long_exchange, short_exchange)` check if there's an active event or if a new one should start

### Event state machine

```python
@dataclass
class ActiveEvent:
    symbol: str
    long_exchange: str
    short_exchange: str
    started_at: datetime
    threshold_pct: float
    
    # Updated on each tick
    samples: list[tuple[datetime, float]]  # (timestamp, spread_pct)
    bbo_long_samples: list[float]
    bbo_short_samples: list[float]
    peak_spread_pct: float
    peaked_at: datetime
    last_above_threshold_at: datetime  # for cooldown tracking
    last_tick_recorded_at: datetime    # for 200ms sampling
```

When spread drops below threshold: don't close immediately. Start cooldown timer. If spread stays below for `cooldown` seconds → finalize event and write to DB. If spread goes back above → cancel cooldown, continue tracking.

### Periodic stats logging

Every 60 seconds, log:
- Number of active events right now
- Total events recorded so far
- Events per minute rate
- Top 3 current active events by spread

### Graceful shutdown

On SIGINT/SIGTERM:
- Close all active events (write them to DB with `ended_at=now`)
- Close all WS connections
- Close DB connection
- Log final summary

### Fee calculation for simulated PnL

Read taker fees from Settings (same `.env` as bot):
```python
roundtrip_cost_pct = 2 * fee_long_pct + 2 * fee_short_pct + slippage_buffer_pct + safety_buffer_pct
simulated_net_pnl_pct = simulated_gross_pnl_pct - roundtrip_cost_pct
```

## File structure

Create ONE file: `scripts/spread_lifecycle.py`. It should be runnable as a module:
```bash
cd src && python -m scripts.spread_lifecycle --threshold 0.10
```

Wait — the scripts are at the repo root level in `scripts/`, not inside `src/`. So the script needs to add `src/` to sys.path to import from `spread_arb`. Follow the pattern used by existing scripts like `scripts/discover_symbols.py`.

Check how `scripts/discover_symbols.py` handles imports and follow the same pattern.

## What NOT to do

- Do NOT modify any existing files
- Do NOT add dependencies — use only what's in `requirements.txt`
- Do NOT connect to exchange REST APIs for trading — this is read-only WS observation
- Do NOT implement any trading logic
- Do NOT use pandas or heavy libraries for the collector itself (keep it lightweight for 24h+ runs)
- Do NOT store raw WS messages — only processed spread data

## Testing

Add a simple smoke test: `tests/test_spread_lifecycle.py` that:
1. Tests the event state machine logic (start, update, peak tracking, cooldown, close)
2. Tests spread calculation matches the main bot's formula
3. Tests DB schema creation and event insertion
