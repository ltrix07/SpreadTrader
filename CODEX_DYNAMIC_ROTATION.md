# Task: Dynamic symbol rotation per trading session

## Problem

The bot uses a static symbol list from `.env`. Meme coins and low-cap tokens have volatile spread profiles — they may show great spreads during one session and nothing during another. Currently we manually add/remove symbols, which is slow and error-prone. We need automatic discovery and rotation of high-spread candidates while keeping a stable base of reliable symbols.

## Architecture overview

Two symbol lists:
1. **Base symbols** — from `SYMBOLS` in `.env`, traded always (BTC, ETH, SOL, etc.)
2. **Dynamic symbols** — discovered automatically by the bot via live spread scanning, rotated at the start of each trading session. Max 15 dynamic symbols.

Three trading sessions (UTC):
- Asia:   00:30 UTC
- Europe: 08:30 UTC  
- US:     16:30 UTC

The +30min offset allows markets to develop activity after session open, so the scan catches real spread opportunities rather than flat pre-session markets.

## Files to modify

### 1. `src/spread_arb/config.py`

Add new settings after `mr_max_bbo_spread_bps` (line 86):

```python
# Dynamic symbol rotation
dynamic_rotation_enabled: bool = False
dynamic_max_symbols: int = Field(default=15, ge=0)
dynamic_min_spread_pct: float = Field(default=0.08, ge=0)
dynamic_max_bbo_bps: float = Field(default=15.0, ge=0)
dynamic_session_times_utc: list[str] = Field(default_factory=lambda: ["00:30", "08:30", "16:30"])
dynamic_scan_timeout_sec: float = Field(default=15.0, gt=0)
dynamic_retry_interval_sec: float = Field(default=30.0, gt=0)
dynamic_require_exchanges: int = Field(default=3, ge=2)
```

Add a validator for `dynamic_session_times_utc`:
```python
@field_validator("dynamic_session_times_utc", mode="before")
@classmethod
def _parse_session_times(cls, value: Any) -> Any:
    parsed = cls._parse_list_env(value)
    if isinstance(parsed, list):
        return [item.strip() for item in parsed]
    return parsed
```

### 2. `src/spread_arb/ws_feeds/base.py`

Add two methods to `WebSocketFeed`:

```python
async def subscribe_symbols(self, symbols: list[str]) -> None:
    """Subscribe to additional symbols on the live WS connection."""
    if not symbols or self._ws is None or self._ws.closed:
        return
    self.symbols.extend(symbols)
    for msg in self._build_subscribe_messages_for(symbols):
        await self._ws.send_str(msg)
        await asyncio.sleep(0.1)
    self.log.info("subscribed to %d new symbols: %s", len(symbols), symbols[:5])

async def unsubscribe_symbols(self, symbols: list[str]) -> None:
    """Unsubscribe from symbols on the live WS connection."""
    if not symbols or self._ws is None or self._ws.closed:
        return
    symbols_set = set(symbols)
    self.symbols = [s for s in self.symbols if s not in symbols_set]
    for msg in self._build_unsubscribe_messages_for(symbols):
        await self._ws.send_str(msg)
        await asyncio.sleep(0.1)
    self.log.info("unsubscribed from %d symbols: %s", len(symbols), symbols[:5])
```

Add two new abstract methods:

```python
@abstractmethod
def _build_subscribe_messages_for(self, symbols: list[str]) -> list[str]:
    """Build subscribe messages for a subset of symbols (for hot-reload)."""
    raise NotImplementedError

@abstractmethod
def _build_unsubscribe_messages_for(self, symbols: list[str]) -> list[str]:
    """Build unsubscribe messages for a subset of symbols."""
    raise NotImplementedError
```

**Default implementation**: `_build_subscribe_messages_for` can save/restore `self.symbols`, call `_build_subscribe_messages`, then restore. But it's cleaner to implement per exchange. See below.

### 3. `src/spread_arb/ws_feeds/binance_ws.py`

Add methods:

```python
def _build_subscribe_messages_for(self, symbols: list[str]) -> list[str]:
    binance_symbols = [self._to_binance(s) for s in symbols]
    params = [f"{s.lower()}@bookTicker" for s in binance_symbols]
    messages = []
    batch_size = 50
    for i in range(0, len(params), batch_size):
        batch = params[i : i + batch_size]
        messages.append(json.dumps({
            "method": "SUBSCRIBE",
            "params": batch,
            "id": 1000 + i,
        }))
    return messages

def _build_unsubscribe_messages_for(self, symbols: list[str]) -> list[str]:
    binance_symbols = [self._to_binance(s) for s in symbols]
    params = [f"{s.lower()}@bookTicker" for s in binance_symbols]
    messages = []
    batch_size = 50
    for i in range(0, len(params), batch_size):
        batch = params[i : i + batch_size]
        messages.append(json.dumps({
            "method": "UNSUBSCRIBE",
            "params": batch,
            "id": 2000 + i,
        }))
    return messages
```

### 4. `src/spread_arb/ws_feeds/bybit_ws.py`

Add methods:

```python
def _build_subscribe_messages_for(self, symbols: list[str]) -> list[str]:
    bybit_symbols = [self._to_bybit(s) for s in symbols]
    args = [f"orderbook.1.{s}" for s in bybit_symbols]
    messages = []
    batch_size = 10
    for i in range(0, len(args), batch_size):
        batch = args[i : i + batch_size]
        messages.append(json.dumps({"op": "subscribe", "args": batch}))
    return messages

def _build_unsubscribe_messages_for(self, symbols: list[str]) -> list[str]:
    bybit_symbols = [self._to_bybit(s) for s in symbols]
    args = [f"orderbook.1.{s}" for s in bybit_symbols]
    messages = []
    batch_size = 10
    for i in range(0, len(args), batch_size):
        batch = args[i : i + batch_size]
        messages.append(json.dumps({"op": "unsubscribe", "args": batch}))
    return messages
```

### 5. `src/spread_arb/ws_feeds/bitget_ws.py`

Add methods:

```python
def _build_subscribe_messages_for(self, symbols: list[str]) -> list[str]:
    bitget_symbols = [self._to_bitget(s) for s in symbols]
    args = [{"instType": "USDT-FUTURES", "channel": "ticker", "instId": s} for s in bitget_symbols]
    messages = []
    batch_size = 30
    for i in range(0, len(args), batch_size):
        batch = args[i : i + batch_size]
        messages.append(json.dumps({"op": "subscribe", "args": batch}))
    return messages

def _build_unsubscribe_messages_for(self, symbols: list[str]) -> list[str]:
    bitget_symbols = [self._to_bitget(s) for s in symbols]
    args = [{"instType": "USDT-FUTURES", "channel": "ticker", "instId": s} for s in bitget_symbols]
    messages = []
    batch_size = 30
    for i in range(0, len(args), batch_size):
        batch = args[i : i + batch_size]
        messages.append(json.dumps({"op": "unsubscribe", "args": batch}))
    return messages
```

### 6. `src/spread_arb/ws_feeds/gate_ws.py`

Add methods:

```python
def _build_subscribe_messages_for(self, symbols: list[str]) -> list[str]:
    contracts = [self._to_gate_contract(s) for s in symbols]
    messages = []
    batch_size = self.subscribe_batch_size
    for i in range(0, len(contracts), batch_size):
        batch = contracts[i : i + batch_size]
        messages.append(json.dumps({
            "time": int(time.time()),
            "channel": "futures.book_ticker",
            "event": "subscribe",
            "payload": batch,
        }))
    return messages

def _build_unsubscribe_messages_for(self, symbols: list[str]) -> list[str]:
    contracts = [self._to_gate_contract(s) for s in symbols]
    messages = []
    batch_size = self.subscribe_batch_size
    for i in range(0, len(contracts), batch_size):
        batch = contracts[i : i + batch_size]
        messages.append(json.dumps({
            "time": int(time.time()),
            "channel": "futures.book_ticker",
            "event": "unsubscribe",
            "payload": batch,
        }))
    return messages
```

### 7. New file: `src/spread_arb/symbol_rotator.py`

Create a new module that handles the rotation logic. This is the core of the feature.

```python
"""Dynamic symbol rotation — discover and rotate high-spread candidates per trading session."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import aiohttp

from .config import Settings

logger = logging.getLogger(__name__)
```

#### 7a. Discovery function (extract from `scripts/discover_symbols.py`)

Create an async function `discover_candidates()` that:
1. Takes an `aiohttp.ClientSession`, `min_spread_pct`, `max_bbo_bps`, `min_exchanges`, `max_symbols`
2. Fetches USDT perp listings from all 4 exchanges (reuse the exact same API calls as `scripts/discover_symbols.py`: `fetch_binance_symbols`, `fetch_bybit_symbols`, `fetch_bitget_symbols`, `fetch_gate_symbols`)
3. Finds symbols on ≥ `min_exchanges` exchanges
4. Fetches live quotes from all 4 exchanges (reuse the quote fetcher logic from `scripts/discover_symbols.py`)
5. Calculates cross-exchange spreads and BBO width (same `calc_spreads` logic)
6. Filters by `max_bbo_bps` and `min_spread_pct`
7. Returns top `max_symbols` sorted by `max_spread_pct` descending
8. Returns `list[str]` — just the symbol names

Copy the fetch functions and spread calculation directly from `scripts/discover_symbols.py` into this module. The functions to copy are:
- `normalize_symbol()`
- `fetch_binance_symbols()`, `fetch_bybit_symbols()`, `fetch_bitget_symbols()`, `fetch_gate_symbols()`
- `fetch_binance_quotes()`, `fetch_bybit_quotes()`, `fetch_bitget_quotes()`, `fetch_gate_quotes()`
- `SymbolSpread` dataclass
- `calc_spreads()`

Then add the orchestrating function:

```python
async def discover_candidates(
    session: aiohttp.ClientSession,
    base_symbols: set[str],
    min_spread_pct: float = 0.08,
    max_bbo_bps: float = 15.0,
    min_exchanges: int = 3,
    max_symbols: int = 15,
) -> list[str]:
    """Discover top spread candidates across exchanges.
    
    Returns symbol names (e.g. ["TACUSDT", "ALCHUSDT"]) excluding base_symbols.
    """
    # ... fetch listings, find common, fetch quotes, calc spreads, filter, sort, return top N
    # Exclude symbols already in base_symbols from results
```

Important: wrap each exchange fetch in try/except so one exchange failure doesn't kill the whole scan. Use `asyncio.gather(return_exceptions=True)` like the original script does.

#### 7b. SymbolRotator class

```python
class SymbolRotator:
    """Manages dynamic symbol rotation per trading session."""

    def __init__(
        self,
        settings: Settings,
        get_open_position_count: Callable[[], int],
        get_pending_entry_count: Callable[[], int],
        on_symbols_changed: Callable[[list[str], list[str]], Awaitable[None]],
    ) -> None:
        self.settings = settings
        self.log = logging.getLogger(__name__)
        self._get_open_position_count = get_open_position_count
        self._get_pending_entry_count = get_pending_entry_count
        self._on_symbols_changed = on_symbols_changed  # callback(added, removed)
        self.scanning = False  # True during scan — trading should pause
        self.current_dynamic_symbols: set[str] = set()
        self.last_rotation_at: datetime | None = None
        self._session_times: list[tuple[int, int]] = []  # [(hour, minute), ...]
        self._parse_session_times()

    def _parse_session_times(self) -> None:
        for time_str in self.settings.dynamic_session_times_utc:
            parts = time_str.strip().split(":")
            self._session_times.append((int(parts[0]), int(parts[1])))

    async def run(self, stop_event: asyncio.Event, http_session: aiohttp.ClientSession) -> None:
        """Main loop: wait for session boundaries, then rotate if conditions met."""
        self.log.info(
            "symbol rotator started | max_dynamic=%d | sessions=%s",
            self.settings.dynamic_max_symbols,
            self.settings.dynamic_session_times_utc,
        )

        while not stop_event.is_set():
            # Sleep until next session time
            now = datetime.now(UTC)
            next_rotation = self._next_session_time(now)
            wait_seconds = (next_rotation - now).total_seconds()

            if wait_seconds > 0:
                self.log.info(
                    "next rotation at %s (in %.0f min)",
                    next_rotation.strftime("%H:%M UTC"),
                    wait_seconds / 60,
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
                    return  # stop_event set
                except TimeoutError:
                    pass

            # Time to rotate — wait for no open positions
            await self._wait_for_no_positions(stop_event)
            if stop_event.is_set():
                return

            # Perform rotation
            await self._do_rotation(http_session)

    async def _wait_for_no_positions(self, stop_event: asyncio.Event) -> None:
        """Wait until there are no open positions or pending entries."""
        retry_sec = self.settings.dynamic_retry_interval_sec
        while not stop_event.is_set():
            open_count = self._get_open_position_count()
            pending_count = self._get_pending_entry_count()
            if open_count == 0 and pending_count == 0:
                return
            self.log.info(
                "rotation waiting | open=%d pending=%d | retry in %.0fs",
                open_count, pending_count, retry_sec,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=retry_sec)
                return
            except TimeoutError:
                pass

    async def _do_rotation(self, http_session: aiohttp.ClientSession) -> None:
        """Execute the rotation: scan, diff, notify."""
        self.scanning = True
        self.log.info("rotation scan starting...")
        try:
            base_symbols = set(self.settings.symbols)
            new_dynamic = await discover_candidates(
                session=http_session,
                base_symbols=base_symbols,
                min_spread_pct=self.settings.dynamic_min_spread_pct,
                max_bbo_bps=self.settings.dynamic_max_bbo_bps,
                min_exchanges=self.settings.dynamic_require_exchanges,
                max_symbols=self.settings.dynamic_max_symbols,
            )

            new_set = set(new_dynamic)
            old_set = self.current_dynamic_symbols

            added = sorted(new_set - old_set)
            removed = sorted(old_set - new_set)

            self.current_dynamic_symbols = new_set
            self.last_rotation_at = datetime.now(UTC)

            self.log.info(
                "rotation complete | dynamic=%d | added=%s | removed=%s",
                len(new_set),
                added if added else "none",
                removed if removed else "none",
            )

            if added or removed:
                await self._on_symbols_changed(added, removed)

        except Exception as exc:
            self.log.error("rotation scan failed: %s", exc, exc_info=True)
        finally:
            self.scanning = False

    def _next_session_time(self, now: datetime) -> datetime:
        """Find the next session rotation time."""
        candidates = []
        for hour, minute in self._session_times:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            candidates.append(candidate)
        return min(candidates)

    def get_all_active_symbols(self) -> list[str]:
        """Return base + dynamic symbols (deduplicated)."""
        base = set(self.settings.symbols)
        return sorted(base | self.current_dynamic_symbols)
```

### 8. `src/spread_arb/scanner.py`

Integrate the rotator into `QuoteScanner`:

#### 8a. Import

Add at the top:
```python
from .symbol_rotator import SymbolRotator
```

#### 8b. Add rotator initialization in `run()`

After MR engine creation (after line ~113), add:

```python
self.symbol_rotator: SymbolRotator | None = None
if self.settings.dynamic_rotation_enabled:
    self.symbol_rotator = SymbolRotator(
        settings=self.settings,
        get_open_position_count=lambda: len(
            self.mean_reversion_engine.open_positions_by_symbol
        ) if self.mean_reversion_engine else 0,
        get_pending_entry_count=lambda: len(
            self.mean_reversion_engine.pending_entries_by_symbol
        ) if self.mean_reversion_engine else 0,
        on_symbols_changed=self._on_dynamic_symbols_changed,
    )
```

#### 8c. Start rotator task

After `snapshot_task` creation (around line 163), add:

```python
rotator_task: asyncio.Task | None = None
if self.symbol_rotator is not None:
    rotator_task = asyncio.create_task(
        self.symbol_rotator.run(self.stop_event, session),
        name="symbol-rotator",
    )
```

Add `rotator_task` to `bg_tasks` list for cleanup.

#### 8d. Store WS feeds for hot-reload

Currently `_start_ws_feeds` creates tasks but doesn't store feed references. Change it to also store the feed objects:

```python
self._ws_feeds: list[WebSocketFeed] = []
```

In `_start_ws_feeds`, store each `feed` in `self._ws_feeds` before creating the task.

#### 8e. Add `_on_dynamic_symbols_changed` callback

```python
async def _on_dynamic_symbols_changed(self, added: list[str], removed: list[str]) -> None:
    """Called by SymbolRotator when dynamic symbols change."""
    # Subscribe to new symbols on all WS feeds
    if added:
        for feed in self._ws_feeds:
            try:
                await feed.subscribe_symbols(added)
            except Exception as exc:
                self.log.warning("failed to subscribe %s on %s: %s", added, feed.name.value, exc)

    # Unsubscribe from removed symbols on all WS feeds
    if removed:
        for feed in self._ws_feeds:
            try:
                await feed.unsubscribe_symbols(removed)
            except Exception as exc:
                self.log.warning("failed to unsubscribe %s on %s: %s", removed, feed.name.value, exc)

        # Clean up stale quotes and baselines for removed symbols
        removed_set = set(removed)
        for sym in removed_set:
            self.latest_quotes_by_symbol.pop(sym, None)
            self.latest_raw_spread_by_symbol.pop(sym, None)
            self.latest_best_opportunity_by_symbol.pop(sym, None)
            # Remove quote entries
            keys_to_remove = [k for k in self.latest_quotes if k[1] == sym]
            for k in keys_to_remove:
                self.latest_quotes.pop(k, None)
                self.last_quote_at.pop(k, None)

        # Clean up MR baselines for removed symbols
        if self.mean_reversion_engine is not None:
            keys_to_remove = [
                k for k in self.mean_reversion_engine.baselines
                if k[0] in removed_set
            ]
            for k in keys_to_remove:
                del self.mean_reversion_engine.baselines[k]

    self.log.info(
        "dynamic symbols updated | active=%d (base=%d + dynamic=%d)",
        len(self.symbol_rotator.get_all_active_symbols()) if self.symbol_rotator else len(self.settings.symbols),
        len(self.settings.symbols),
        len(self.symbol_rotator.current_dynamic_symbols) if self.symbol_rotator else 0,
    )
```

#### 8f. Pause trading during scan

In `_scan_symbol()` method (line 226), add at the very beginning:

```python
def _scan_symbol(self, symbol: str) -> None:
    # Pause signal evaluation during dynamic rotation scan
    if self.symbol_rotator is not None and self.symbol_rotator.scanning:
        return
    # ... rest of existing code
```

This effectively pauses trade signal evaluation during the ~3-5 second discovery scan. WS messages still arrive and update quotes, but no new trades are evaluated.

### 9. `src/spread_arb/ws_feeds/__init__.py`

No changes needed — the WS feed classes are already exported.

### 10. Also handle OKX and HTX WS feeds

If `src/spread_arb/ws_feeds/okx_ws.py` and `src/spread_arb/ws_feeds/htx_ws.py` exist, add `_build_subscribe_messages_for` and `_build_unsubscribe_messages_for` methods to them as well. Follow the same pattern as the other exchanges.

For OKX: `{"op": "subscribe"/"unsubscribe", "args": [{"channel": "...", "instId": "..."}]}`
For HTX: follow the existing subscribe format in the HTX WS feed.

## Important constraints

1. **Do NOT modify `scripts/discover_symbols.py`** — the rotator copies the discovery logic into `symbol_rotator.py`. The CLI script stays as-is for manual use.

2. **Do NOT modify the MR engine's `_evaluate_signal()` or `check_exits()`** — the pause mechanism is in `scanner._scan_symbol()`, not in the MR engine.

3. **Do NOT change existing symbol handling in `.env`** — `SYMBOLS` in config stays as the base list. Dynamic symbols are additive.

4. **`dynamic_rotation_enabled` defaults to `False`** — the feature is opt-in. Existing behavior unchanged when disabled.

5. **Never remove a symbol that has an open position** — this is guaranteed by `_wait_for_no_positions()` which blocks until `open_positions_by_symbol` and `pending_entries_by_symbol` are both empty.

6. **Use `asyncio.gather(return_exceptions=True)`** for all parallel exchange API calls in discover, same as the original script.

7. **The `_build_subscribe_messages_for` / `_build_unsubscribe_messages_for` methods must NOT be abstract** — provide a default implementation in the base class that temporarily swaps `self.symbols`, calls `_build_subscribe_messages()`, and restores. This way if a new exchange feed is added without implementing these methods, it still works. Each exchange subclass can override for cleaner implementation.

## Testing

After implementation:
1. `python -m compileall src/spread_arb/` should pass
2. With `DYNAMIC_ROTATION_ENABLED=false` (default), behavior is identical to current
3. With `DYNAMIC_ROTATION_ENABLED=true`, the rotator should:
   - Log next rotation time on startup
   - Wait for session boundary + check no open positions
   - Run discovery scan (~3-5 seconds)
   - Subscribe/unsubscribe WS feeds for changed symbols
   - Log added/removed symbols
   - Resume trading with updated symbol universe
