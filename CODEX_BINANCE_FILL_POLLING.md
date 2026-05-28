# Fix: Binance order response parsing returns 0 for avg_price/filled_qty

## Real incident timeline (from production logs 2026-05-28)

The bot opened a live spread position on NEARUSDT (long binance + short bitget) and immediately hit a series of CRITICAL failures because Binance returned a market order response with `avgPrice=0` and `executedQty=0` despite the position actually filling on the exchange. The user had to close the position manually.

Exact log sequence:

```
13:37:44 | INFO | mr open | NEARUSDT | long=binance @ 0.000000 | short=bitget @ 2.337100 |
              notional=$10.20 | spread=+0.3475% | mean=+0.0027% | sigma=6.77 | target=+0.0027%

13:37:45 | CRITICAL | LIVE entry missing protective stops | NEARUSDT | 
              long_stop=<missing> short_stop=1443887241819578368 | closing position immediately

13:37:48 | CRITICAL | EXIT LONG LEG FAILED | NEARUSDT | 
              Binance API error: {'code': -4003, 'msg': 'Quantity less than or equal to zero.'} 
              - MANUAL CLOSE NEEDED

13:37:48 | CRITICAL | EXIT FAILED and position still open | NEARUSDT | MANUAL INTERVENTION
13:37:48 | CRITICAL | LIVE position still open after protective-stop failure | NEARUSDT | 
              manual intervention may be required
```

Then for **~3 hours**, the bot kept retrying close every few minutes:

```
13:45:40, 13:45:45, 13:45:52, 13:45:56, 14:10:32, 16:24:32, 16:24:37, 16:24:42 ...

Each retry:
  EXIT LONG LEG FAILED | NEARUSDT | Binance API error: {'code': -4003, 'msg': 'Quantity less than or equal to zero.'}
  EXIT SHORT LEG FAILED | NEARUSDT | Bitget API error: {'code': '22002', 'msg': 'No position to close'}
  EXIT FAILED and position still open | NEARUSDT | MANUAL INTERVENTION
```

User confirmed:
- The long position on Binance **was actually open and accumulating PnL**. User closed it manually at +$0.15.
- Bitget short was already closed by the time emergency exit ran (probably never opened, or filled+closed quickly via the stop_order that has a non-zero ID in line 1).
- No entry was ever recorded in `paper_trades` table — the trade is lost from local accounting.

## Root cause

Binance Futures `POST /fapi/v1/order` with `type=MARKET` sometimes returns immediately with the order acknowledged but **not yet filled**, meaning the response contains:
- `status` = `NEW` or `PARTIALLY_FILLED`
- `avgPrice` = `"0.00000000"`
- `executedQty` = `"0"`
- `cumQuote` = `"0"`

The bot's `place_market_order` implementation in `src/spread_arb/exchanges/binance.py` parses these zero values into `OrderResult.avg_price` and `OrderResult.filled_qty` and returns. Downstream:

1. `MeanReversionEngine._execute_after_delay` stores `actual_qty_long = Decimal("0")` in `MeanRevPosition`.
2. `entry_long_price = 0.0` is logged as `mr open ... long=binance @ 0.000000`.
3. `place_protective_stops` either fails (qty=0) or skips silently.
4. Subsequent close attempts call `execute_spread_exit(long_qty=Decimal("0"), ...)` → Binance rejects with `-4003 Quantity less than or equal to zero`.
5. Bot retries forever, the position lives on Binance unmonitored.

## Files to fix

1. **`src/spread_arb/exchanges/binance.py`** — `place_market_order` must poll order status until filled, OR fall back to position lookup when fill data is missing.
2. **`src/spread_arb/exchanges/base.py`** — may need a `get_order(symbol, order_id)` abstract method if it doesn't exist (verify; if it does, reuse it).
3. **`src/spread_arb/mean_reversion_engine.py`** — `_execute_after_delay` must validate `actual_qty_long`/`actual_qty_short > 0` before storing position state; if not, trigger emergency cleanup.
4. **`tests/test_binance_fill_polling.py`** — new test file.

Verify the same issue doesn't exist in `bybit.py`, `okx.py`, `gate.py`, `bitget.py`, `mexc.py`. Other exchanges may have similar timing — if so, apply the same pattern.

## Fix 1: Polling order until filled in `place_market_order`

In `src/spread_arb/exchanges/binance.py`, after placing the market order:

```python
async def place_market_order(self, symbol: str, side: str, qty: Decimal, close: bool = False) -> OrderResult:
    # ... existing code that posts the order and gets initial response ...
    initial_response = await self._post_order(...)
    order_id = str(initial_response["orderId"])

    # NEW: Poll until filled or timeout
    final = await self._wait_for_fill(symbol, order_id, timeout_sec=5.0)
    return self._parse_order_response(final, symbol, side)
```

Implementation of `_wait_for_fill`:

```python
async def _wait_for_fill(self, symbol: str, order_id: str, timeout_sec: float = 5.0) -> dict:
    """Poll GET /fapi/v1/order until status is FILLED, EXPIRED, CANCELED, or REJECTED.

    Returns the final order dict. Raises on timeout if status is still NEW/PARTIALLY_FILLED.
    """
    deadline = time.monotonic() + timeout_sec
    poll_interval = 0.2  # 200ms
    last_response: dict = {}

    while time.monotonic() < deadline:
        try:
            response = await self._get_order(symbol, order_id)
            last_response = response
            status = response.get("status", "")
            if status in {"FILLED", "EXPIRED", "CANCELED", "REJECTED"}:
                return response
        except Exception as exc:
            self.log.warning("poll order failed | %s %s | %s", symbol, order_id, exc)
        await asyncio.sleep(poll_interval)

    # Timeout — return last response even if not FILLED. Caller decides what to do.
    self.log.warning(
        "order poll timeout | %s %s | last_status=%s executedQty=%s",
        symbol, order_id,
        last_response.get("status"), last_response.get("executedQty"),
    )
    return last_response
```

`_get_order` calls `GET /fapi/v1/order?symbol={symbol}&orderId={order_id}` (signed request).

## Fix 2: Robust parsing — handle remaining edge case where status is FILLED but avgPrice/cumQuote inconsistent

Some Binance responses return `status=FILLED, executedQty>0` but `avgPrice="0"`. In that case calculate from `cumQuote / executedQty`:

```python
def _parse_order_response(self, response: dict, symbol: str, side: str) -> OrderResult:
    filled_qty = Decimal(response.get("executedQty", "0"))
    avg_price_str = response.get("avgPrice", "0")
    avg_price = Decimal(avg_price_str)
    cum_quote = Decimal(response.get("cumQuote", "0"))

    # Fix: if avgPrice is 0 but we have fill data, derive from cumQuote
    if avg_price <= 0 and filled_qty > 0 and cum_quote > 0:
        avg_price = cum_quote / filled_qty

    return OrderResult(
        exchange=ExchangeName.BINANCE,
        symbol=symbol,
        side=side,
        filled_qty=filled_qty,
        avg_price=avg_price,
        # ... other fields
    )
```

## Fix 3: Final safety net — fallback to `get_position()` in engine if parse still returns zero

In `src/spread_arb/mean_reversion_engine.py:_execute_after_delay`, after `execute_spread_entry()` returns:

```python
entry_long_price = float(spread_result.long_order.avg_price)
entry_short_price = float(spread_result.short_order.avg_price)
actual_qty_long = spread_result.long_order.filled_qty
actual_qty_short = spread_result.short_order.filled_qty

# NEW: If either leg looks invalid, verify via get_position
if entry_long_price <= 0 or actual_qty_long <= 0:
    self.log.warning(
        "mr entry | %s | long order parse degraded (price=%s qty=%s), checking actual position",
        symbol, entry_long_price, actual_qty_long,
    )
    try:
        long_pos = await self.execution_service.clients[current.long_exchange].get_position(symbol)
        if abs(float(long_pos.size)) > 0:
            actual_qty_long = abs(long_pos.size)
            entry_long_price = float(long_pos.entry_price)
            self.log.info(
                "mr entry | %s | recovered from get_position | long_qty=%s long_price=%s",
                symbol, actual_qty_long, entry_long_price,
            )
        else:
            self.log.critical(
                "mr entry | %s | long order returned zero AND no position on exchange | aborting",
                symbol,
            )
            # Try to close short leg if it filled
            if actual_qty_short > 0:
                try:
                    await self.execution_service.clients[current.short_exchange].place_market_order(
                        symbol, "buy", actual_qty_short, close=True,
                    )
                except Exception as exc:
                    self.log.critical("failed to close orphan short leg | %s | %s", symbol, exc)
            return
    except Exception as exc:
        self.log.critical("mr entry | %s | get_position failed during recovery | %s", symbol, exc)
        return

# Same logic for short leg
if entry_short_price <= 0 or actual_qty_short <= 0:
    # ... mirror of long-leg recovery ...
```

## Fix 4: Close path resilience

In `_close_position` when live exit fails with "Quantity less than or equal to zero" (or similar zero-qty errors), the existing code already checks `get_position()` to see if position was already closed by an exchange stop. That logic is correct.

But we should also handle the case where `position.actual_qty_long > 0` (we think we have a position) but Binance says qty is 0. In that case, the bot should:

1. Query `get_position()` directly to see if real position exists.
2. If real position exists with non-zero size, use **that size** (not the stored size which may be 0 from earlier broken parse) for close.
3. If no real position, mark the trade as closed (close_reason="already_closed") and record what we know in paper_trades for accountability.

This prevents the infinite retry loop. After 1-2 attempts of cleanup and still failing, exit the close-retry loop and log a critical alert.

In `mean_reversion_engine.py:_close_position`, add a retry counter on the position to limit close attempts. After N failed attempts (e.g., 3), force-remove from `open_positions_by_symbol` and log critical "manual intervention required, position removed from tracking." This prevents infinite spam in logs.

## Tests in `tests/test_binance_fill_polling.py`

```python
@pytest.mark.asyncio
async def test_wait_for_fill_returns_filled_status():
    """Order eventually becomes FILLED — _wait_for_fill returns final state."""
    client = BinanceClient(...)
    mock_responses = [
        {"status": "NEW", "executedQty": "0", "avgPrice": "0"},
        {"status": "PARTIALLY_FILLED", "executedQty": "5", "avgPrice": "1.20"},
        {"status": "FILLED", "executedQty": "10", "avgPrice": "1.25"},
    ]
    # mock _get_order to return these in sequence
    ...
    result = await client._wait_for_fill("BTCUSDT", "12345", timeout_sec=2.0)
    assert result["status"] == "FILLED"
    assert result["executedQty"] == "10"


@pytest.mark.asyncio
async def test_wait_for_fill_timeout_returns_last_response():
    """If status never becomes FILLED within timeout, return last response."""
    # mock returns NEW indefinitely
    ...
    result = await client._wait_for_fill("BTCUSDT", "12345", timeout_sec=0.5)
    assert result["status"] == "NEW"


@pytest.mark.asyncio  
async def test_parse_order_response_derives_avg_price_from_cum_quote():
    """If avgPrice=0 but executedQty and cumQuote are non-zero, calculate avg = cumQuote/executedQty."""
    response = {"status": "FILLED", "executedQty": "10", "avgPrice": "0", "cumQuote": "12.50"}
    result = client._parse_order_response(response, "BTCUSDT", "buy")
    assert result.filled_qty == Decimal("10")
    assert result.avg_price == Decimal("1.25")  # 12.50 / 10


@pytest.mark.asyncio
async def test_engine_recovers_position_from_get_position_when_order_returns_zero():
    """Even if order parse fails, engine recovers actual_qty via get_position()."""
    # Setup engine with mocked execution service:
    # - execute_spread_entry returns OrderResult with avg_price=0, filled_qty=0
    # - get_position returns Position with size=5, entry_price=100
    # Run _execute_after_delay
    # Assert: open_positions_by_symbol has entry with qty=5, price=100
    ...


@pytest.mark.asyncio
async def test_engine_aborts_when_zero_parse_and_no_real_position():
    """If order parse fails AND get_position returns 0 size, abort entry cleanly."""
    # Setup: both parsing returns 0 AND get_position returns 0
    # Run _execute_after_delay
    # Assert: open_positions_by_symbol stays empty
    # Assert: no infinite retries
    ...


@pytest.mark.asyncio
async def test_close_retry_limit_prevents_infinite_loop():
    """After N failed close attempts, position is force-removed and critical logged."""
    # Setup position with broken qty
    # Mock execute_spread_exit to always fail
    # Trigger close repeatedly
    # Assert: position removed after N attempts
    # Assert: critical log fired
    ...
```

## Acceptance criteria

1. All existing tests pass (clock injection, smart_exits, funding_filter, etc.).
2. New tests in `test_binance_fill_polling.py` pass (minimum 6).
3. `python -m compileall src/spread_arb` passes.
4. The specific scenario from logs is reproducible in tests:
   - When `place_market_order` returns avg_price=0/filled_qty=0
   - And the position is actually open on the exchange
   - The bot recovers via `get_position()` and tracks the trade correctly
5. Infinite close-retry loop is bounded (max 3-5 attempts, then force-remove + critical log).
6. No changes to scanner.py, storage.py, paper_engine.py logic — only execution recovery paths.

## Don't do

- Don't change the spread strategy logic itself.
- Don't add a generic polling mechanism to **all** exchange clients reflexively. Only fix Binance now (and verify by reading code if any other exchange has the same parse-zero pattern; if yes, apply identical fix there too).
- Don't add new metrics or telemetry beyond what's needed for the recovery logic.
- Don't restructure base.py interfaces beyond adding `get_order()` if it's truly missing.

## Verify other exchanges

While in this fix, read these files quickly and confirm whether they have the same risk:
- `src/spread_arb/exchanges/bybit.py` — `place_market_order` parsing
- `src/spread_arb/exchanges/okx.py` — same
- `src/spread_arb/exchanges/gate.py` — same
- `src/spread_arb/exchanges/bitget.py` — same
- `src/spread_arb/exchanges/mexc.py` — same

If any of them parse `avgPrice` directly from the immediate place-order response without status check, they have the same bug and need the same polling pattern. Report in the diff which exchanges you applied the fix to and which you decided don't need it (with reason).

## After completion

Send diff. I'll do a control review, then we'll deploy:
1. Apply fix on server
2. Run pytest tests/ -v — confirm all green
3. Restart bot in **paper mode first** (LIVE_TRADING=false) for a few hours to confirm no regression
4. Then re-enable live trading with current safety settings
