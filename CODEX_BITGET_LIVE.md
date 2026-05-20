# Task: Add Live Trading Support for Bitget

## Overview

Live trading was implemented for Binance, OKX, Bybit, and MEXC but Bitget was skipped. Bitget is one of our most profitable exchange pairs, so it needs full live trading support. Bitget API requires a passphrase (like OKX).

## Reference

Look at how OKX is implemented for the passphrase pattern, and how Binance/Bybit are implemented for the trading methods. Follow the same patterns.

## Step 1: Config — `src/spread_arb/config.py`

Add after `api_secret_bitget`:
```python
    api_key_bitget: str = ""
    api_secret_bitget: str = ""
    api_passphrase_bitget: str = ""
```

Check if `api_key_bitget` and `api_secret_bitget` already exist. If so, just add the passphrase field.

## Step 2: Scanner credentials — `src/spread_arb/scanner.py`

Find where the credential map is built (look for `_build_live_credential_map` or where OKX/Binance/Bybit/MEXC credentials are mapped). Add Bitget:

```python
ExchangeName.BITGET: {
    "api_key": settings.api_key_bitget,
    "api_secret": settings.api_secret_bitget,
    "passphrase": settings.api_passphrase_bitget,
},
```

Also add Bitget to the credential validation check so it fails fast if keys are missing.

## Step 3: Bitget Exchange Constructor — `src/spread_arb/exchanges/bitget.py`

Add passphrase support to constructor (same pattern as OKX):

```python
def __init__(self, session, request_timeout_sec=8.0, api_key="", api_secret="", passphrase="", **kwargs):
    super().__init__(session, request_timeout_sec, api_key, api_secret, **kwargs)
    self.passphrase = passphrase
```

## Step 4: Signing — `src/spread_arb/exchanges/bitget.py`

Bitget V2 API signing:
- HMAC-SHA256-Base64 of `timestamp + method + requestPath + body`
- Headers: `ACCESS-KEY`, `ACCESS-SIGN`, `ACCESS-TIMESTAMP`, `ACCESS-PASSPHRASE`
- Base URL: `https://api.bitget.com`

```python
from ..exchanges.signing import hmac_sha256_base64, timestamp_ms

async def _signed_request(self, method: str, path: str, body: dict | None = None) -> dict:
    """Make a signed request to Bitget API."""
    import json as _json
    ts = str(int(time.time() * 1000))
    body_str = _json.dumps(body) if body else ""
    sign_msg = f"{ts}{method.upper()}{path}{body_str}"
    signature = hmac_sha256_base64(self.api_secret, sign_msg)
    
    headers = {
        "ACCESS-KEY": self.api_key,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": self.passphrase,
        "Content-Type": "application/json",
        "locale": "en-US",
    }
    
    url = f"{self.base_url}{path}"
    if method.upper() == "GET":
        async with self.session.get(url, headers=headers, timeout=self.request_timeout_sec) as resp:
            data = await resp.json()
    else:
        async with self.session.post(url, headers=headers, data=body_str, timeout=self.request_timeout_sec) as resp:
            data = await resp.json()
    
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget API error: {data}")
    return data.get("data", data)
```

## Step 5: Trading Methods — `src/spread_arb/exchanges/bitget.py`

Bitget V2 Mix API endpoints for USDT-M futures:

### place_market_order
```
POST /api/v2/mix/order/place-order
Body: {
    "symbol": "BTCUSDT",      # Bitget symbol format
    "productType": "USDT-FUTURES",
    "marginMode": "crossed",
    "marginCoin": "USDT",
    "side": "buy" or "sell",   # buy/sell
    "tradeSide": "open" or "close",  # open for entry, close for exit
    "orderType": "market",
    "size": "0.01",            # quantity in base asset
}
```

Response contains `orderId`. To get fill details:
```
GET /api/v2/mix/order/detail?symbol=BTCUSDT&productType=USDT-FUTURES&orderId=xxx
```

Fill details contain `priceAvg` (average fill price), `baseVolume` (filled qty), `fee`.

**IMPORTANT:** The `side` + `tradeSide` combination:
- Entry long: side="buy", tradeSide="open"
- Entry short: side="sell", tradeSide="open"
- Exit long (sell): side="sell", tradeSide="close"
- Exit short (buy back): side="buy", tradeSide="close"

The current `place_market_order(symbol, side, qty)` interface doesn't distinguish open vs close. You need to determine this from context. The simplest approach: add an optional `trade_side` parameter defaulting to "open", and pass "close" from execution service when exiting.

OR: Bitget also supports `side` values "buy" and "sell" with `tradeSide` "open"/"close". For simplicity, you can try using `reduceOnly=True` for close orders instead of `tradeSide="close"`. But the explicit tradeSide approach is more reliable.

**Recommended approach:** Override the base class method signature slightly:

```python
async def place_market_order(self, symbol: str, side: str, qty: Decimal, close: bool = False) -> OrderResult:
    bitget_symbol = self._to_bitget_symbol(symbol)
    body = {
        "symbol": bitget_symbol,
        "productType": "USDT-FUTURES",
        "marginMode": "crossed",
        "marginCoin": "USDT",
        "side": side.lower(),
        "tradeSide": "close" if close else "open",
        "orderType": "market",
        "size": str(qty),
    }
    data = await self._signed_request("POST", "/api/v2/mix/order/place-order", body)
    order_id = data.get("orderId", "")
    
    # Fetch fill details
    import asyncio
    await asyncio.sleep(0.5)
    detail = await self._signed_request("GET", f"/api/v2/mix/order/detail?symbol={bitget_symbol}&productType=USDT-FUTURES&orderId={order_id}")
    
    return OrderResult(
        exchange=self.name,
        symbol=symbol,
        side=side.lower(),
        filled_qty=Decimal(detail.get("baseVolume", "0")),
        avg_price=Decimal(detail.get("priceAvg", "0")),
        fee=abs(Decimal(detail.get("fee", "0"))),
        fee_currency="USDT",
        order_id=order_id,
        timestamp=datetime.now(UTC),
        is_partial=detail.get("state") != "filled",
        raw_response=detail,
    )
```

### set_leverage
```
POST /api/v2/mix/account/set-leverage
Body: {
    "symbol": "BTCUSDT",
    "productType": "USDT-FUTURES",
    "marginCoin": "USDT",
    "leverage": "3",
    "holdSide": "long"   # must be called twice: once for "long", once for "short"
}
```

**Note:** Bitget requires setting leverage separately for long and short sides:
```python
async def set_leverage(self, symbol: str, leverage: int) -> None:
    bitget_symbol = self._to_bitget_symbol(symbol)
    for hold_side in ("long", "short"):
        try:
            await self._signed_request("POST", "/api/v2/mix/account/set-leverage", {
                "symbol": bitget_symbol,
                "productType": "USDT-FUTURES",
                "marginCoin": "USDT",
                "leverage": str(leverage),
                "holdSide": hold_side,
            })
        except RuntimeError as exc:
            # Ignore "leverage already set" errors
            if "leverage" not in str(exc).lower():
                raise
```

### get_balance
```
GET /api/v2/mix/account/accounts?productType=USDT-FUTURES
```
Parse the response for USDT account. Fields: `usdtEquity` (total), `crossedMaxAvailable` (available).

```python
async def get_balance(self) -> BalanceInfo:
    data = await self._signed_request("GET", "/api/v2/mix/account/accounts?productType=USDT-FUTURES")
    if isinstance(data, list):
        for item in data:
            if item.get("marginCoin") == "USDT":
                return BalanceInfo(
                    exchange=self.name,
                    total_usdt=Decimal(item.get("usdtEquity", "0")),
                    available_usdt=Decimal(item.get("crossedMaxAvailable", item.get("available", "0"))),
                )
    raise RuntimeError("USDT balance not found on Bitget")
```

### get_position
```
GET /api/v2/mix/position/single-position?symbol=BTCUSDT&productType=USDT-FUTURES&marginCoin=USDT
```

```python
async def get_position(self, symbol: str) -> PositionInfo:
    bitget_symbol = self._to_bitget_symbol(symbol)
    data = await self._signed_request(
        "GET",
        f"/api/v2/mix/position/single-position?symbol={bitget_symbol}&productType=USDT-FUTURES&marginCoin=USDT",
    )
    if isinstance(data, list) and data:
        pos = data[0]
        size = Decimal(pos.get("total", "0"))
        if pos.get("holdSide") == "short":
            size = -size
        return PositionInfo(
            exchange=self.name,
            symbol=symbol,
            size=size,
            entry_price=Decimal(pos.get("openPriceAvg", "0")),
            unrealized_pnl=Decimal(pos.get("unrealizedPL", "0")),
            leverage=int(pos.get("leverage", "1")),
        )
    # No position
    return PositionInfo(
        exchange=self.name, symbol=symbol, size=Decimal("0"),
        entry_price=Decimal("0"), unrealized_pnl=Decimal("0"), leverage=1,
    )
```

### get_min_order_qty
```
GET /api/v2/mix/market/contracts?productType=USDT-FUTURES&symbol=BTCUSDT
```
Parse `minTradeNum` field.

### place_stop_market_order
```
POST /api/v2/mix/order/place-plan-order
Body: {
    "symbol": "BTCUSDT",
    "productType": "USDT-FUTURES",
    "marginMode": "crossed",
    "marginCoin": "USDT",
    "side": "sell",  # opposite of position
    "tradeSide": "close",
    "orderType": "market",
    "size": "0.01",
    "triggerPrice": "95000",
    "triggerType": "mark_price",
}
```
Returns `orderId`.

### cancel_order
```
POST /api/v2/mix/order/cancel-plan-order
Body: {
    "orderId": "xxx",
    "symbol": "BTCUSDT",
    "productType": "USDT-FUTURES",
    "marginCoin": "USDT",
}
```

For regular orders (not plan/stop):
```
POST /api/v2/mix/order/cancel-order
```

## Step 6: Execution Service — `src/spread_arb/execution.py`

The execution service calls `place_market_order(symbol, side, qty)`. For Bitget, closing requires `close=True`. There are two approaches:

**Option A (preferred):** In `execute_spread_exit()`, detect if the client is Bitget and pass `close=True`. This is simple since we know it's an exit:

```python
# In execute_spread_exit, when calling place_market_order for each leg:
kwargs = {"close": True} if isinstance(client, BitgetExchange) else {}
await client.place_market_order(symbol, side, qty, **kwargs)
```

Or better: add `close: bool = False` to the base class `place_market_order` signature so all connectors accept it but only Bitget uses it.

**Option B:** Add `close=False` parameter to base class `place_market_order()` and pass `close=True` from `execute_spread_exit()` for ALL exchanges. Other exchanges just ignore the parameter.

Go with **Option B** — it's cleaner. Update `base.py`:
```python
async def place_market_order(self, symbol: str, side: str, qty: Decimal, close: bool = False) -> OrderResult:
```

And in `execute_spread_exit()`, always pass `close=True`:
```python
long_client.place_market_order(symbol, "sell", long_qty, close=True)
short_client.place_market_order(symbol, "buy", short_qty, close=True)
```

Update ALL existing connectors (Binance, OKX, Bybit, MEXC) to accept the `close` parameter (they can ignore it):
```python
async def place_market_order(self, symbol: str, side: str, qty: Decimal, close: bool = False) -> OrderResult:
```

## Step 7: Update test script — `scripts/test_execution.py`

Add "bitget" to the exchange choices. Add passphrase extraction for Bitget in `get_credentials()`.

## Files to change

| File | Action | Description |
|------|--------|-------------|
| `src/spread_arb/config.py` | MODIFY | Add `api_passphrase_bitget` field (check if api_key/secret already exist) |
| `src/spread_arb/scanner.py` | MODIFY | Add Bitget to credential map with passphrase |
| `src/spread_arb/exchanges/base.py` | MODIFY | Add `close: bool = False` to `place_market_order` signature |
| `src/spread_arb/exchanges/bitget.py` | MODIFY | Add constructor with passphrase, `_signed_request`, all trading methods |
| `src/spread_arb/exchanges/binance.py` | MODIFY | Add `close: bool = False` param to `place_market_order` (ignore it) |
| `src/spread_arb/exchanges/bybit.py` | MODIFY | Same |
| `src/spread_arb/exchanges/okx.py` | MODIFY | Same |
| `src/spread_arb/exchanges/mexc.py` | MODIFY | Same — but MEXC actually needs it for side mapping (side 4 vs 1) |
| `src/spread_arb/execution.py` | MODIFY | Pass `close=True` in `execute_spread_exit()` |
| `scripts/test_execution.py` | MODIFY | Add bitget support |

## Verification

1. `python -m py_compile src/spread_arb/exchanges/bitget.py` — no errors
2. Paper mode with Bitget in EXCHANGES list — should still work for quotes
3. `python scripts/test_execution.py --exchange bitget --symbol SOLUSDT --notional 10` — round-trip test
