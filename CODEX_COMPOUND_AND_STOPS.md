# Task: Auto-Compounding + Exchange-Side Stop-Loss Protection

Two features in one task. Both modify the same files (execution.py, engine, config, exchange connectors).

---

## Part A: Auto-Compounding (Dynamic Notional Sizing)

### Overview

Currently the bot uses a fixed `MR_NOTIONAL_USDT` for every trade. Add compound mode: before each trade, the bot checks available balance on both exchanges and uses a percentage of the minimum as notional. Position size grows automatically with profits.

### A1. Config — `src/spread_arb/config.py`

Add these fields to `Settings`:

```python
    mr_compound_enabled: bool = True
    mr_notional_pct: float = Field(default=85.0, gt=0, le=100)  # % of available balance per leg
    mr_min_notional_usdt: float = Field(default=5.0, ge=1)  # floor — don't trade below this
```

When `mr_compound_enabled=True`, the bot uses `mr_notional_pct` of the available balance instead of the fixed `mr_notional_usdt`. The fixed value becomes a fallback for paper trading mode only.

Also change:
```python
    default_leverage: int = Field(default=3, ge=1, le=125)  # was default=1
```

### A2. Execution Service — `src/spread_arb/execution.py`

Add balance cache and dynamic notional calculation:

```python
# In __init__:
self._balance_cache: dict[ExchangeName, tuple[float, float]] = {}  # exchange -> (available_usdt, timestamp)
self._balance_cache_ttl: float = 30.0  # seconds

async def _get_cached_balance(self, exchange: ExchangeName) -> float:
    """Get available balance with 30s cache to avoid hammering API."""
    import time
    now = time.time()
    cached = self._balance_cache.get(exchange)
    if cached and (now - cached[1]) < self._balance_cache_ttl:
        return cached[0]
    
    balance = await self.clients[exchange].get_balance()
    available = float(balance.available_usdt)
    self._balance_cache[exchange] = (available, now)
    return available

def invalidate_balance_cache(self, exchange: ExchangeName) -> None:
    """Clear cached balance after a trade fills (so next calc uses fresh data)."""
    self._balance_cache.pop(exchange, None)

async def calculate_notional(
    self,
    long_exchange: ExchangeName,
    short_exchange: ExchangeName,
) -> float:
    """Calculate notional per leg based on available balance on both exchanges.
    
    Returns notional in USDT, or 0.0 if balance is insufficient.
    """
    if not self.settings.mr_compound_enabled:
        return self.settings.mr_notional_usdt

    try:
        long_avail = await self._get_cached_balance(long_exchange)
        short_avail = await self._get_cached_balance(short_exchange)
    except Exception as exc:
        self.log.warning("balance fetch failed for notional calc: %s — using fixed notional", exc)
        return self.settings.mr_notional_usdt

    # Use the smaller available balance as the constraint
    min_available = min(long_avail, short_avail)
    
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
    
    self.log.info(
        "compound notional: $%.2f (%.0f%% of min balance $%.2f)",
        notional, self.settings.mr_notional_pct, min_available,
    )
    return notional
```

**After every successful entry or exit**, call `invalidate_balance_cache()` for both exchanges so the next trade uses a fresh balance:

```python
# At end of execute_spread_entry(), after successful fill:
self.invalidate_balance_cache(long_exchange)
self.invalidate_balance_cache(short_exchange)

# At end of execute_spread_exit(), after successful fill:
self.invalidate_balance_cache(long_exchange)
self.invalidate_balance_cache(short_exchange)
```

### A3. Engine — `src/spread_arb/mean_reversion_engine.py`

In `_execute_after_delay()`, in the live mode branch, BEFORE placing orders:

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

Then use `notional` everywhere in the method instead of `self.settings.mr_notional_usdt`:
- Pass to `execute_spread_entry(notional_usdt=notional, ...)`
- Use for fee/slippage estimation
- Store in `MeanRevPosition.notional_usdt`
- Log it: `notional=$%.2f`

In paper mode with compound enabled, log a startup note:
```python
if self.settings.mr_compound_enabled and not self.live_mode:
    self.log.info("compound enabled but paper mode — using fixed notional $%.2f", self.settings.mr_notional_usdt)
```

---

## Part B: Exchange-Side Stop-Loss Orders

### Overview

Currently stop-loss only exists in bot logic on the server. If the bot crashes, API goes down, or the server hangs — positions stay open with leverage and no protection. Fix: place a real stop-market order on each exchange immediately after opening each leg. This is a "catastrophic protection" — a wide stop that catches disasters, NOT the bot's normal spread-based stop.

### B1. Config — `src/spread_arb/config.py`

Add:
```python
    exchange_stop_loss_pct: float = Field(default=5.0, gt=0, le=50)  # % from entry price for exchange-side stop
```

This means: if a leg moves 5% against us, the exchange closes it automatically. With 3x leverage and 5% stop, max loss per leg = 15% of notional. This is the emergency parachute, not the normal exit.

### B2. Exchange Connectors — Add stop order methods

Add to `src/spread_arb/exchanges/base.py`:

```python
async def place_stop_market_order(
    self, symbol: str, side: str, qty: Decimal, stop_price: Decimal
) -> str:
    """Place a stop-market order. Returns order_id. Override in subclass."""
    raise NotImplementedError(f"{self.name.value} does not support stop orders")

async def cancel_order(self, symbol: str, order_id: str) -> None:
    """Cancel an open order. Override in subclass."""
    raise NotImplementedError(f"{self.name.value} does not support order cancellation")
```

#### Binance — `src/spread_arb/exchanges/binance.py`

```python
async def place_stop_market_order(
    self, symbol: str, side: str, qty: Decimal, stop_price: Decimal
) -> str:
    bn_symbol = self._to_binance_symbol(symbol)
    params = {
        "symbol": bn_symbol,
        "side": side.upper(),  # BUY or SELL (opposite of position direction)
        "type": "STOP_MARKET",
        "stopPrice": str(stop_price),
        "quantity": str(self._to_exchange_qty(symbol, qty)),
        "closePosition": "false",
    }
    data = await self._signed_request("POST", "/fapi/v1/order", params)
    return str(data.get("orderId", ""))

async def cancel_order(self, symbol: str, order_id: str) -> None:
    bn_symbol = self._to_binance_symbol(symbol)
    await self._signed_request("DELETE", "/fapi/v1/order", {
        "symbol": bn_symbol,
        "orderId": order_id,
    })
```

#### Bybit — `src/spread_arb/exchanges/bybit.py`

```python
async def place_stop_market_order(
    self, symbol: str, side: str, qty: Decimal, stop_price: Decimal
) -> str:
    bb_symbol = self._to_bybit_symbol(symbol)
    # Bybit uses triggerPrice + orderType=Market for stop-market
    params = {
        "category": "linear",
        "symbol": bb_symbol,
        "side": "Buy" if side.lower() == "buy" else "Sell",
        "orderType": "Market",
        "qty": str(qty),
        "triggerPrice": str(stop_price),
        "triggerDirection": 2 if side.lower() == "buy" else 1,
        # triggerDirection: 1=triggered when price rises to triggerPrice, 2=falls to triggerPrice
        # For a long position stop (sell): trigger when price FALLS -> triggerDirection=2... 
        # Actually: triggerDirection=1 means "triggered when market price rises to trigger price"
        # triggerDirection=2 means "triggered when market price falls to trigger price"
        # Long stop (sell when price drops): triggerDirection=2
        # Short stop (buy when price rises): triggerDirection=1
    }
    data = await self._signed_request("POST", "/v5/order/create", params)
    return data.get("orderId", "")

async def cancel_order(self, symbol: str, order_id: str) -> None:
    bb_symbol = self._to_bybit_symbol(symbol)
    await self._signed_request("POST", "/v5/order/cancel", {
        "category": "linear",
        "symbol": bb_symbol,
        "orderId": order_id,
    })
```

**Bybit triggerDirection clarification:**
- Long position → stop sells when price DROPS → `triggerDirection=2` (triggered when price falls to triggerPrice)
- Short position → stop buys when price RISES → `triggerDirection=1` (triggered when price rises to triggerPrice)

#### OKX — `src/spread_arb/exchanges/okx.py`

```python
async def place_stop_market_order(
    self, symbol: str, side: str, qty: Decimal, stop_price: Decimal
) -> str:
    inst_id = self._to_okx_inst_id(symbol)
    # OKX uses algo orders for stop-loss
    body = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": side.lower(),  # "buy" or "sell"
        "ordType": "trigger",  # trigger order = stop order on OKX
        "triggerPx": str(stop_price),
        "orderPx": "-1",  # -1 means market price (market stop)
        "sz": str(qty),
        "triggerPxType": "last",  # trigger on last traded price
    }
    data = await self._signed_request("POST", "/api/v5/trade/order-algo", body)
    if data and isinstance(data, list) and len(data) > 0:
        return data[0].get("algoId", "")
    return ""

async def cancel_order(self, symbol: str, order_id: str) -> None:
    inst_id = self._to_okx_inst_id(symbol)
    # OKX algo orders use a different cancel endpoint
    body = [{"instId": inst_id, "algoId": order_id}]
    await self._signed_request("POST", "/api/v5/trade/cancel-algos", body)
```

**OKX note:** Stop orders on OKX are "algo orders" — they use `/api/v5/trade/order-algo` for placement and `/api/v5/trade/cancel-algos` for cancellation. The `algoId` returned is different from a normal `ordId`.

#### MEXC — `src/spread_arb/exchanges/mexc.py`

```python
async def place_stop_market_order(
    self, symbol: str, side: str, qty: Decimal, stop_price: Decimal
) -> str:
    mexc_symbol = self._to_mexc_symbol(symbol)
    # MEXC futures: use planOrder for stop orders
    # side mapping for close:
    # Close long (sell stop) = side 4
    # Close short (buy stop) = side 2
    if side.lower() == "sell":
        mexc_side = 4  # close long
    else:
        mexc_side = 2  # close short
    
    params = {
        "symbol": mexc_symbol,
        "side": mexc_side,
        "type": 5,  # market
        "triggerPrice": str(stop_price),
        "triggerType": 1,  # 1=last price trigger
        "vol": int(qty),  # MEXC uses contract count
        "openType": 2,  # cross margin
    }
    data = await self._signed_request("POST", "/api/v1/private/planorder/place", params)
    return str(data) if data else ""

async def cancel_order(self, symbol: str, order_id: str) -> None:
    mexc_symbol = self._to_mexc_symbol(symbol)
    await self._signed_request("POST", "/api/v1/private/planorder/cancel", {
        "symbol": mexc_symbol,
        "orderId": order_id,
    })
```

**MEXC note:** MEXC futures uses "plan orders" for conditional/stop orders. The endpoint is `/api/v1/private/planorder/place`. `vol` is in contract count (integer), not base asset qty.

### B3. Execution Service — `src/spread_arb/execution.py`

Add stop-loss placement and cancellation to the execution flow.

Add a new method:

```python
async def place_protective_stops(
    self,
    symbol: str,
    long_exchange: ExchangeName,
    short_exchange: ExchangeName,
    long_qty: Decimal,
    short_qty: Decimal,
    long_entry_price: Decimal,
    short_entry_price: Decimal,
) -> tuple[str, str]:
    """Place exchange-side stop orders on both legs as catastrophic protection.
    
    Returns (long_stop_order_id, short_stop_order_id).
    """
    stop_pct = self.settings.exchange_stop_loss_pct / 100.0
    
    # Long leg: stop sells if price drops X% below entry
    long_stop_price = long_entry_price * Decimal(str(1.0 - stop_pct))
    # Short leg: stop buys if price rises X% above entry
    short_stop_price = short_entry_price * Decimal(str(1.0 + stop_pct))
    
    long_stop_id = ""
    short_stop_id = ""
    
    try:
        long_stop_id = await self.clients[long_exchange].place_stop_market_order(
            symbol=symbol,
            side="sell",  # sell to close long
            qty=long_qty,
            stop_price=long_stop_price,
        )
        self.log.info(
            "protective stop placed | %s | LONG %s | stop @ %s (-%s%%)",
            symbol, long_exchange.value, long_stop_price, self.settings.exchange_stop_loss_pct,
        )
    except Exception as exc:
        self.log.error("failed to place long protective stop | %s | %s | %s", symbol, long_exchange.value, exc)
    
    try:
        short_stop_id = await self.clients[short_exchange].place_stop_market_order(
            symbol=symbol,
            side="buy",  # buy to close short
            qty=short_qty,
            stop_price=short_stop_price,
        )
        self.log.info(
            "protective stop placed | %s | SHORT %s | stop @ %s (+%s%%)",
            symbol, short_exchange.value, short_stop_price, self.settings.exchange_stop_loss_pct,
        )
    except Exception as exc:
        self.log.error("failed to place short protective stop | %s | %s | %s", symbol, short_exchange.value, exc)
    
    return long_stop_id, short_stop_id

async def cancel_protective_stops(
    self,
    symbol: str,
    long_exchange: ExchangeName,
    short_exchange: ExchangeName,
    long_stop_id: str,
    short_stop_id: str,
) -> None:
    """Cancel exchange-side stop orders when bot closes the position normally."""
    if long_stop_id:
        try:
            await self.clients[long_exchange].cancel_order(symbol, long_stop_id)
            self.log.debug("cancelled long protective stop | %s | %s", symbol, long_exchange.value)
        except Exception as exc:
            self.log.warning("failed to cancel long stop | %s | %s | %s", symbol, long_exchange.value, exc)
    
    if short_stop_id:
        try:
            await self.clients[short_exchange].cancel_order(symbol, short_stop_id)
            self.log.debug("cancelled short protective stop | %s | %s", symbol, short_exchange.value)
        except Exception as exc:
            self.log.warning("failed to cancel short stop | %s | %s | %s", symbol, short_exchange.value, exc)
```

### B4. Engine Integration — `src/spread_arb/mean_reversion_engine.py`

#### Add stop order IDs to MeanRevPosition

Add fields to the `MeanRevPosition` dataclass (or wherever position state is stored):

```python
long_stop_order_id: str = ""
short_stop_order_id: str = ""
```

#### In `_execute_after_delay()` — place stops after entry

In the live mode branch, AFTER successful entry fill:

```python
if self.live_mode:
    # ... existing entry logic ...
    
    # Place protective stops on both exchanges
    long_stop_id, short_stop_id = await self.execution_service.place_protective_stops(
        symbol=symbol,
        long_exchange=current.long_exchange,
        short_exchange=current.short_exchange,
        long_qty=spread_result.long_order.filled_qty,
        short_qty=spread_result.short_order.filled_qty,
        long_entry_price=spread_result.long_order.avg_price,
        short_entry_price=spread_result.short_order.avg_price,
    )
```

Store these IDs in the position object:
```python
position.long_stop_order_id = long_stop_id
position.short_stop_order_id = short_stop_id
```

#### In `_close_position()` — cancel stops before exit

In the live mode branch, BEFORE placing exit orders:

```python
if self.live_mode:
    # Cancel protective stops first (so they don't conflict with our exit orders)
    await self.execution_service.cancel_protective_stops(
        symbol=position.symbol,
        long_exchange=position.long_exchange,
        short_exchange=position.short_exchange,
        long_stop_id=position.long_stop_order_id,
        short_stop_id=position.short_stop_order_id,
    )
    
    # ... existing exit logic ...
```

**Important ordering:** Cancel stops BEFORE placing exit market orders. Otherwise the stop might trigger during exit execution and double-close the position.

### B5. Handle edge case: exchange stop triggers before bot exits

If the bot is slow and the exchange stop fires first, the bot's exit order will fail because the position is already closed. The bot should handle this gracefully:

In `_close_position()`, if the exit order fails, check if the position is already flat:

```python
except ExecutionError as exc:
    # Position might have been closed by exchange stop — check
    self.log.warning("exit order failed, checking if position closed by exchange stop | %s | %s", position.symbol, exc)
    try:
        long_pos = await self.execution_service.clients[position.long_exchange].get_position(position.symbol)
        short_pos = await self.execution_service.clients[position.short_exchange].get_position(position.symbol)
        if float(long_pos.size) == 0 and float(short_pos.size) == 0:
            self.log.info("position already closed (exchange stop triggered) | %s", position.symbol)
            # Proceed with PnL recording — use last known prices
            # ... continue to record the trade ...
        else:
            self.log.critical("EXIT FAILED and position still open | %s | MANUAL INTERVENTION", position.symbol)
            return
    except Exception:
        self.log.critical("EXIT FAILED, could not verify position state | %s | MANUAL INTERVENTION", position.symbol)
        return
```

---

## Update `.env.example`

The Mean Reversion section should now look like:

```
# --- Mean Reversion Strategy ---
MR_ENABLED=true
MR_SIGMA_ENTRY=2.5
MR_SIGMA_STOP=6.0
MR_MIN_STOP_DISTANCE_PCT=0.15
MR_MIN_NET_EDGE_PCT=0.25
MR_TAKE_PROFIT_FRACTION=0.75
MR_NOTIONAL_USDT=10
MR_COMPOUND_ENABLED=true
MR_NOTIONAL_PCT=85
MR_MIN_NOTIONAL_USDT=5
MR_MAX_POSITIONS=1
MR_ROLLING_WINDOW=360
MR_MAX_HOLD_SECONDS=900
MR_COOLDOWN_SEC=120
MR_EXIT_MAX_QUOTE_AGE_MS=30000
MR_EXCLUDED_EXCHANGES=htx
MR_QUOTE_FRESHNESS_WINDOW=30
MR_MIN_QUOTE_FRESHNESS_PCT=80

# --- Live Trading ---
LIVE_TRADING=true
DEFAULT_LEVERAGE=3
ORDER_TIMEOUT_SEC=10.0
MAX_NOTIONAL_USDT=50
BALANCE_SNAPSHOT_INTERVAL_SEC=1800
EXCHANGE_STOP_LOSS_PCT=5.0
```

---

## File Change Summary

| File | Action | Description |
|------|--------|-------------|
| `src/spread_arb/config.py` | MODIFY | Add `mr_compound_enabled`, `mr_notional_pct`, `mr_min_notional_usdt`, `exchange_stop_loss_pct`. Change `default_leverage` default to 3. |
| `src/spread_arb/exchanges/base.py` | MODIFY | Add `place_stop_market_order()`, `cancel_order()` default methods |
| `src/spread_arb/exchanges/binance.py` | MODIFY | Implement `place_stop_market_order()`, `cancel_order()` |
| `src/spread_arb/exchanges/bybit.py` | MODIFY | Implement `place_stop_market_order()`, `cancel_order()` |
| `src/spread_arb/exchanges/okx.py` | MODIFY | Implement `place_stop_market_order()`, `cancel_order()` (algo orders) |
| `src/spread_arb/exchanges/mexc.py` | MODIFY | Implement `place_stop_market_order()`, `cancel_order()` (plan orders) |
| `src/spread_arb/execution.py` | MODIFY | Add `calculate_notional()`, `_get_cached_balance()`, balance cache, `invalidate_balance_cache()`, `place_protective_stops()`, `cancel_protective_stops()` |
| `src/spread_arb/mean_reversion_engine.py` | MODIFY | Use dynamic notional in live branch. Add stop order IDs to position. Place stops after entry, cancel before exit. Handle exchange-stop-triggered-first edge case. |
| `.env.example` | MODIFY | Add compound + stop settings |

## Verification

1. **Paper mode unchanged**: `LIVE_TRADING=false` — behavior identical to current, no regression
2. **Compound in live mode**: Logs show `compound notional: $X.XX` with correct percentage calculation
3. **Compound after trade**: After a profitable trade, next notional should be slightly higher
4. **Compound floor**: If balance drops below $5.88 ($5 / 0.85), bot skips trades
5. **Protective stops placed**: After entry, logs show `protective stop placed | SYMBOL | LONG exchange | stop @ PRICE`
6. **Stops cancelled on normal exit**: After mean_reversion close, logs show `cancelled ... protective stop`
7. **Test with test_execution.py**: Modify test script to also test stop placement and cancellation:
   - Place a market order
   - Place a stop order
   - Cancel the stop order
   - Close the market order
