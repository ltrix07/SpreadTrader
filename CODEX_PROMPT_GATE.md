# Task: Implement Gate.io futures live trading methods

## Context

We have a crypto spread arbitrage bot that trades perpetual USDT-margined futures across multiple exchanges. Gate.io (`src/spread_arb/exchanges/gate.py`) currently only has `fetch_quote()` for reading orderbook data. We need to add all live trading methods so the bot can execute real trades on Gate.

The bot already works live on Binance, Bybit, Bitget, and MEXC. Use those implementations as reference — especially `bitget.py` (cleanest, most similar API style) and `mexc.py` (contract-based sizing similar to Gate).

## Gate.io Futures API

- **Base URL:** `https://api.gateio.ws`
- **API docs:** https://www.gate.io/docs/developers/futures/index.html
- **Auth:** HMAC-SHA512 signature. Headers: `KEY`, `SIGN`, `Timestamp`. Sign string: `{method}\n{path}\n{query_string}\n{sha512(body)}\n{timestamp}`
- **Contract names:** underscore format like `BTC_USDT` (already handled by `_to_gate_contract()`)
- **Sizes:** Gate uses **contracts**, not base currency. Contract size varies per instrument (e.g., BTC_USDT contract_size=0.0001 BTC). Must query `/api/v4/futures/usdt/contracts/{contract}` for `quanto_multiplier` / contract sizing.
- **Order sides:** For futures: `"buy"` to open long / close short, `"sell"` to open short / close long. Use `"close": true` or `"reduce_only": true` to close positions.
- **Leverage:** `PUT /api/v4/futures/usdt/positions/{contract}/leverage` with `leverage` param

## Files to modify

### 1. `src/spread_arb/exchanges/gate.py` — Main implementation

Add the following methods to `GateExchange(ExchangeClient)`:

#### `_signed_request(self, method, path, query_params=None, body=None) -> dict`
- Gate V4 auth: HMAC-SHA512
- Timestamp: Unix epoch seconds as string
- Sign string: `"{METHOD}\n{URL_PATH}\n{QUERY_STRING}\n{SHA512_HEX(BODY)}\n{TIMESTAMP}"`
- Headers: `KEY: {api_key}`, `SIGN: {signature}`, `Timestamp: {ts}`, `Content-Type: application/json`
- For GET: query params go in URL, body is empty string
- For POST/PUT/DELETE: body is JSON string
- Return parsed JSON response
- Raise RuntimeError on error responses (Gate returns `label` field in error objects)

#### `get_balance(self) -> BalanceInfo`
- `GET /api/v4/futures/usdt/accounts`
- Returns object with `total`, `available`, `currency`
- Map to `BalanceInfo(exchange=self.name, total_usdt=Decimal(total), available_usdt=Decimal(available))`

#### `get_position(self, symbol) -> PositionInfo`
- `GET /api/v4/futures/usdt/positions/{contract}`
- Returns object with `size` (positive=long, negative=short), `entry_price`, `unrealised_pnl`, `leverage`
- If size=0 or 404, return zero position
- Map to `PositionInfo`

#### `set_leverage(self, symbol, leverage) -> None`
- `POST /api/v4/futures/usdt/positions/{contract}/leverage`
- Body: `{"leverage": str(leverage)}`
- Ignore errors about leverage already being set

#### `get_min_order_qty(self, symbol) -> Decimal`
- Use contract detail endpoint to get minimum order size
- `GET /api/v4/futures/usdt/contracts/{contract}`
- Get `quanto_multiplier` and `order_size_min`
- Return minimum qty in base currency terms (contracts * quanto_multiplier)

#### `place_market_order(self, symbol, side, qty, close=False) -> OrderResult`
- Convert base qty to contracts using contract detail (`quanto_multiplier`)
- `POST /api/v4/futures/usdt/orders`
- Body: `{"contract": contract, "size": signed_size, "price": "0", "tif": "ioc"}`
  - `size` is positive for buy, negative for sell
  - `price: "0"` with `tif: "ioc"` = market order on Gate
  - For closing: add `"close": true` or `"reduce_only": true`
- After placing, fetch order detail to get fill price: `GET /api/v4/futures/usdt/orders/{order_id}`
- **IMPORTANT:** Add 0.5s sleep + retry (up to 3 attempts) when fetching fill price. If fill price is 0, retry. We had a critical bug on MEXC where missing fill price caused fake -$10 losses. See `mexc.py` `_fetch_order_detail_with_retry` for the pattern.
- Map to `OrderResult` with `avg_price`, `filled_qty` (convert contracts back to base), `fee`

#### `place_stop_market_order(self, symbol, side, qty, stop_price) -> str`
- Gate uses price-triggered orders: `POST /api/v4/futures/usdt/price_orders`
- Body structure with trigger rule and order params
- Return order_id string

#### `cancel_order(self, symbol, order_id) -> None`
- `DELETE /api/v4/futures/usdt/price_orders/{order_id}`

### 2. `src/spread_arb/exchanges/signing.py` — Add HMAC-SHA512

Add a new function:
```python
def hmac_sha512_hex(secret: str, message: str) -> str:
    """HMAC-SHA512, return hex digest. Used by Gate.io."""
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
```

Also add a SHA-512 hash helper (Gate needs SHA-512 of request body):
```python
def sha512_hex(message: str) -> str:
    """SHA-512 hash, return hex digest."""
    return hashlib.sha512(message.encode("utf-8")).hexdigest()
```

### 3. `src/spread_arb/config.py` — Add Gate API key settings

Add after line 115 (`api_secret_mexc`):
```python
api_key_gate: str = ""
api_secret_gate: str = ""
```

### 4. `src/spread_arb/scanner.py` — Register Gate API credentials

In `_exchange_credentials()` method (around line 734), add Gate entry:
```python
ExchangeName.GATE: {
    "api_key": settings.api_key_gate,
    "api_secret": settings.api_secret_gate,
},
```

## Reference implementations

- **`bitget.py`** — Best reference for overall structure: `_signed_request`, `place_market_order` with open/close logic, `place_stop_market_order`, `get_balance`, `get_position`, `set_leverage`, `get_min_order_qty`. Bitget uses HMAC-SHA256-Base64; Gate uses HMAC-SHA512-Hex.
- **`mexc.py`** — Reference for contract-based sizing: `_base_qty_to_contracts`, `_contracts_to_base_qty`, `_contract_detail` cache. Gate also uses contracts rather than base currency.
- **`base.py`** — Abstract base class with all method signatures and return types.
- **`models.py`** — `OrderResult`, `BalanceInfo`, `PositionInfo` dataclass definitions.

## Key patterns to follow

1. All methods use `self.session` (aiohttp ClientSession) for HTTP requests
2. All methods use `self.request_timeout_sec` for timeouts
3. All methods use `self.log` for logging
4. `api_key` and `api_secret` are available via `self.api_key` and `self.api_secret` (from base class)
5. Use `Decimal` for all prices and quantities
6. Cache contract details to avoid repeated API calls (see MEXC `_contract_cache` pattern)
7. Log raw response on errors for debugging
8. When fetching order detail after placement, always sleep + retry to handle async fill settlement

## Important: Contract sizing on Gate

Gate futures use contracts. Each contract has a `quanto_multiplier` (= contract size in base currency). For example, if BTC_USDT has quanto_multiplier=0.0001, then 1 contract = 0.0001 BTC. To trade 0.01 BTC, you need 100 contracts.

```python
contracts = int(qty / Decimal(str(quanto_multiplier)))
```

Cache the contract detail per symbol to avoid re-fetching.

## Testing

After implementation, verify with:
```python
# In Python REPL or test script:
import asyncio, aiohttp
from spread_arb.exchanges.gate import GateExchange

async def test():
    async with aiohttp.ClientSession() as session:
        gate = GateExchange(session, api_key="...", api_secret="...")
        balance = await gate.get_balance()
        print(f"Balance: {balance}")
        pos = await gate.get_position("BTCUSDT")
        print(f"Position: {pos}")

asyncio.run(test())
```

## Do NOT

- Do not modify `fetch_quote()` — it works fine
- Do not modify `_to_gate_contract()` — symbol mapping is correct
- Do not modify the WebSocket feed (`GateWsFeed` in `ws/gate.py`) — it's separate
- Do not change any other exchange implementations
- Do not remove the `_STRIP_1000_PREFIX` or `_PRICE_MULTIPLIER` logic
