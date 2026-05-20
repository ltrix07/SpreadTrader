# Task: Add Live Order Execution to SpreadTrader

## Overview

The bot currently only does paper trading (simulated execution). Add real order placement capability for Binance, OKX, Bybit, and MEXC perpetual futures. Keep paper trading fully working — live mode is enabled by `LIVE_TRADING=true` in `.env`.

## Architecture

```
MeanReversionEngine (decides WHAT to trade)
        │
        ▼
ExecutionService (NEW — orchestrates HOW to execute)
        │
        ▼
ExchangeClient.place_market_order() (sends order to exchange API)
```

## Implementation Steps (execute in this exact order)

### Step 1: Create signing utility `src/spread_arb/exchanges/signing.py` (NEW FILE)

Shared HMAC signing functions used by all exchange connectors:

```python
"""HMAC signing utilities for exchange API authentication."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time


def hmac_sha256_hex(secret: str, message: str) -> str:
    """HMAC-SHA256, return hex digest. Used by Binance, Bybit, MEXC."""
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def hmac_sha256_base64(secret: str, message: str) -> str:
    """HMAC-SHA256, return base64 digest. Used by OKX."""
    signature = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(signature).decode("utf-8")


def timestamp_ms() -> int:
    """Current UTC timestamp in milliseconds."""
    return int(time.time() * 1000)


def timestamp_iso() -> str:
    """Current UTC timestamp in ISO format (for OKX)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
```

### Step 2: Add new models to `src/spread_arb/models.py`

Append these dataclasses AFTER the existing ones:

```python
@dataclass(frozen=True, slots=True)
class OrderResult:
    """Result of a single exchange order execution."""
    exchange: ExchangeName
    symbol: str
    side: str           # "buy" or "sell"
    filled_qty: Decimal
    avg_price: Decimal
    fee: Decimal
    fee_currency: str
    order_id: str
    timestamp: datetime
    is_partial: bool    # True if not fully filled
    raw_response: dict  # full exchange response for debugging


@dataclass(frozen=True, slots=True)
class SpreadOrderResult:
    """Result of a spread entry or exit (two legs)."""
    long_order: OrderResult
    short_order: OrderResult


@dataclass(frozen=True, slots=True)
class PositionInfo:
    """Current position on an exchange."""
    exchange: ExchangeName
    symbol: str
    size: Decimal       # positive=long, negative=short, 0=flat
    entry_price: Decimal
    unrealized_pnl: Decimal
    leverage: int


@dataclass(frozen=True, slots=True)
class BalanceInfo:
    """USDT balance on an exchange."""
    exchange: ExchangeName
    total_usdt: Decimal
    available_usdt: Decimal
```

Add necessary imports: `datetime` (if not already imported).

### Step 3: Add config fields to `src/spread_arb/config.py`

Add to the `Settings` class, after the existing fields:

```python
    # Live trading
    live_trading: bool = False
    default_leverage: int = Field(default=1, ge=1, le=125)
    order_timeout_sec: float = Field(default=10.0, gt=0)
    max_notional_usdt: float = Field(default=50.0, ge=0)  # safety cap

    # API credentials
    api_key_binance: str = ""
    api_secret_binance: str = ""
    api_key_okx: str = ""
    api_secret_okx: str = ""
    api_passphrase_okx: str = ""
    api_key_bybit: str = ""
    api_secret_bybit: str = ""
    api_key_mexc: str = ""
    api_secret_mexc: str = ""
```

### Step 4: Update `.env.example`

Append:

```
# Live trading (set to true to enable real order execution)
LIVE_TRADING=false
DEFAULT_LEVERAGE=1
ORDER_TIMEOUT_SEC=10.0
MAX_NOTIONAL_USDT=50

# API Keys (fill in for live trading)
API_KEY_BINANCE=
API_SECRET_BINANCE=
API_KEY_OKX=
API_SECRET_OKX=
API_PASSPHRASE_OKX=
API_KEY_BYBIT=
API_SECRET_BYBIT=
API_KEY_MEXC=
API_SECRET_MEXC=
```

### Step 5: Extend `src/spread_arb/exchanges/base.py`

Add `api_key` and `api_secret` to the constructor (with defaults so existing code doesn't break):

```python
def __init__(self, session: ClientSession, request_timeout_sec: float = 8.0,
             api_key: str = "", api_secret: str = "", **kwargs) -> None:
    ...existing init code...
    self.api_key = api_key
    self.api_secret = api_secret
```

Add default method implementations (NOT abstract — so Gate/Bitget/HTX don't need changes):

```python
async def place_market_order(self, symbol: str, side: str, qty: Decimal) -> "OrderResult":
    """Place a market order. Override in subclass for live trading."""
    raise NotImplementedError(f"{self.name.value} does not support order placement")

async def get_position(self, symbol: str) -> "PositionInfo":
    raise NotImplementedError(f"{self.name.value} does not support position queries")

async def set_leverage(self, symbol: str, leverage: int) -> None:
    raise NotImplementedError(f"{self.name.value} does not support leverage setting")

async def get_balance(self) -> "BalanceInfo":
    raise NotImplementedError(f"{self.name.value} does not support balance queries")

async def get_min_order_qty(self, symbol: str) -> Decimal:
    raise NotImplementedError(f"{self.name.value} does not support min qty queries")
```

Import `Decimal` from decimal module.

### Step 6: Implement trading methods in `src/spread_arb/exchanges/binance.py`

**Signing approach:** HMAC-SHA256 of query string params. Signature appended as `&signature=` param. Header: `X-MBX-APIKEY: {api_key}`.

**Base URL for futures:** `https://fapi.binance.com`

**CRITICAL: Symbol mapping.** The existing `_to_binance_symbol()` method handles symbol conversion. Reuse it. Also handle the `_PRICE_DIVISOR` mapping for 1000-prefixed contracts (PEPE, FLOKI, BONK, SHIB) — when these are traded, the quantity must be adjusted accordingly.

Methods to implement:

```python
async def _signed_request(self, method: str, path: str, params: dict | None = None) -> dict:
    """Make a signed request to Binance Futures API."""
    params = params or {}
    params["timestamp"] = timestamp_ms()
    params["recvWindow"] = 5000
    query = "&".join(f"{k}={v}" for k, v in params.items())
    signature = hmac_sha256_hex(self.api_secret, query)
    query += f"&signature={signature}"
    
    url = f"https://fapi.binance.com{path}?{query}"
    headers = {"X-MBX-APIKEY": self.api_key}
    
    async with self.session.request(method, url, headers=headers, timeout=...) as resp:
        data = await resp.json()
        if resp.status != 200:
            raise Exception(f"Binance API error: {data}")
        return data

async def place_market_order(self, symbol: str, side: str, qty: Decimal) -> OrderResult:
    bn_symbol = self._to_binance_symbol(symbol)
    params = {
        "symbol": bn_symbol,
        "side": side.upper(),  # BUY or SELL
        "type": "MARKET",
        "quantity": str(qty),
    }
    data = await self._signed_request("POST", "/fapi/v1/order", params)
    return OrderResult(
        exchange=self.name,
        symbol=symbol,
        side=side.lower(),
        filled_qty=Decimal(data["executedQty"]),
        avg_price=Decimal(data["avgPrice"]),
        fee=Decimal("0"),  # Binance returns fee in separate endpoint; estimate from config
        fee_currency="USDT",
        order_id=str(data["orderId"]),
        timestamp=datetime.fromtimestamp(data["updateTime"] / 1000, tz=timezone.utc),
        is_partial=data["status"] != "FILLED",
        raw_response=data,
    )

async def set_leverage(self, symbol: str, leverage: int) -> None:
    bn_symbol = self._to_binance_symbol(symbol)
    await self._signed_request("POST", "/fapi/v1/leverage", {
        "symbol": bn_symbol,
        "leverage": leverage,
    })

async def get_balance(self) -> BalanceInfo:
    data = await self._signed_request("GET", "/fapi/v2/balance")
    for item in data:
        if item["asset"] == "USDT":
            return BalanceInfo(
                exchange=self.name,
                total_usdt=Decimal(item["balance"]),
                available_usdt=Decimal(item["availableBalance"]),
            )
    raise Exception("USDT balance not found")

async def get_position(self, symbol: str) -> PositionInfo:
    bn_symbol = self._to_binance_symbol(symbol)
    data = await self._signed_request("GET", "/fapi/v2/positionRisk", {"symbol": bn_symbol})
    for item in data:
        if item["symbol"] == bn_symbol:
            return PositionInfo(
                exchange=self.name,
                symbol=symbol,
                size=Decimal(item["positionAmt"]),
                entry_price=Decimal(item["entryPrice"]),
                unrealized_pnl=Decimal(item["unRealizedProfit"]),
                leverage=int(item["leverage"]),
            )
    raise Exception(f"Position not found for {symbol}")

async def get_min_order_qty(self, symbol: str) -> Decimal:
    bn_symbol = self._to_binance_symbol(symbol)
    url = f"https://fapi.binance.com/fapi/v1/exchangeInfo"
    async with self.session.get(url, timeout=...) as resp:
        data = await resp.json()
    for s in data["symbols"]:
        if s["symbol"] == bn_symbol:
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    return Decimal(f["minQty"])
    raise Exception(f"Min qty not found for {symbol}")
```

### Step 7: Implement trading methods in `src/spread_arb/exchanges/bybit.py`

**Signing approach:** HMAC-SHA256 of `{timestamp}{api_key}{recv_window}{body_or_query}`. Headers: `X-BAPI-API-KEY`, `X-BAPI-SIGN`, `X-BAPI-TIMESTAMP`, `X-BAPI-RECV-WINDOW`.

**Base URL:** `https://api.bybit.com`

**Symbol mapping:** Reuse existing `_to_bybit_symbol()`.

```python
async def _signed_request(self, method: str, path: str, params: dict | None = None) -> dict:
    ts = str(timestamp_ms())
    recv_window = "5000"
    
    if method == "GET":
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        sign_payload = f"{ts}{self.api_key}{recv_window}{query}"
        url = f"https://api.bybit.com{path}?{query}" if query else f"https://api.bybit.com{path}"
        body = None
    else:
        import json as _json
        body = _json.dumps(params or {})
        sign_payload = f"{ts}{self.api_key}{recv_window}{body}"
        url = f"https://api.bybit.com{path}"
    
    signature = hmac_sha256_hex(self.api_secret, sign_payload)
    headers = {
        "X-BAPI-API-KEY": self.api_key,
        "X-BAPI-SIGN": signature,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type": "application/json",
    }
    
    async with self.session.request(method, url, headers=headers, data=body, timeout=...) as resp:
        data = await resp.json()
        if data.get("retCode") != 0:
            raise Exception(f"Bybit API error: {data}")
        return data.get("result", data)

async def place_market_order(self, symbol: str, side: str, qty: Decimal) -> OrderResult:
    bb_symbol = self._to_bybit_symbol(symbol)
    params = {
        "category": "linear",
        "symbol": bb_symbol,
        "side": "Buy" if side.lower() == "buy" else "Sell",
        "orderType": "Market",
        "qty": str(qty),
    }
    data = await self._signed_request("POST", "/v5/order/create", params)
    order_id = data["orderId"]
    
    # Fetch fill details
    import asyncio
    await asyncio.sleep(0.5)  # wait for fill
    fills = await self._signed_request("GET", "/v5/order/realtime", {
        "category": "linear",
        "symbol": bb_symbol,
        "orderId": order_id,
    })
    order_info = fills["list"][0] if fills.get("list") else {}
    
    return OrderResult(
        exchange=self.name,
        symbol=symbol,
        side=side.lower(),
        filled_qty=Decimal(order_info.get("cumExecQty", "0")),
        avg_price=Decimal(order_info.get("avgPrice", "0")),
        fee=Decimal(order_info.get("cumExecFee", "0")),
        fee_currency="USDT",
        order_id=order_id,
        timestamp=datetime.now(timezone.utc),
        is_partial=order_info.get("orderStatus") != "Filled",
        raw_response=order_info,
    )

# set_leverage: POST /v5/position/set-leverage
# body: {"category": "linear", "symbol": bb_symbol, "buyLeverage": str(leverage), "sellLeverage": str(leverage)}
# Note: Bybit returns error 110043 if leverage is already set to the requested value — ignore this error.

# get_balance: GET /v5/account/wallet-balance?accountType=UNIFIED
# Parse result.list[0].coin[] where coinName == "USDT"

# get_position: GET /v5/position/list?category=linear&symbol={bb_symbol}
# Parse result.list[0]

# get_min_order_qty: GET /v5/market/instruments-info?category=linear&symbol={bb_symbol}
# Parse result.list[0].lotSizeFilter.minOrderQty
```

### Step 8: Implement trading methods in `src/spread_arb/exchanges/okx.py`

**Signing approach:** HMAC-SHA256-Base64 of `{timestamp}{method}{requestPath}{body}`. Headers: `OK-ACCESS-KEY`, `OK-ACCESS-SIGN`, `OK-ACCESS-TIMESTAMP`, `OK-ACCESS-PASSPHRASE`.

**CRITICAL:** OKX requires a PASSPHRASE in addition to key+secret. Add `passphrase` to constructor.

**Base URL:** `https://www.okx.com`

**Symbol mapping:** Reuse existing `_to_okx_inst_id()`. OKX uses instId format like `BTC-USDT-SWAP`.

**Quantity:** OKX SWAP contracts use contract counts, not base-asset qty. Each contract has a `ctVal` (contract value). E.g., BTC-USDT-SWAP has ctVal=0.01 BTC. So for $10 notional at BTC=$100k: base_qty = 0.0001 BTC, contracts = 0.0001 / 0.01 = 0.01 contracts. Need to fetch `ctVal` from instruments endpoint and round to `ctMul`.

```python
def __init__(self, session, request_timeout_sec=8.0, api_key="", api_secret="", passphrase="", **kwargs):
    super().__init__(session, request_timeout_sec, api_key, api_secret, **kwargs)
    self.passphrase = passphrase

async def _signed_request(self, method: str, path: str, body: dict | None = None) -> dict:
    ts = timestamp_iso()
    body_str = json.dumps(body) if body else ""
    sign_msg = f"{ts}{method.upper()}{path}{body_str}"
    signature = hmac_sha256_base64(self.api_secret, sign_msg)
    
    headers = {
        "OK-ACCESS-KEY": self.api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": self.passphrase,
        "Content-Type": "application/json",
    }
    
    url = f"https://www.okx.com{path}"
    kwargs = {"headers": headers, "timeout": ...}
    if body:
        kwargs["data"] = json.dumps(body)
    
    async with self.session.request(method, url, **kwargs) as resp:
        data = await resp.json()
        if data.get("code") != "0":
            raise Exception(f"OKX API error: {data}")
        return data.get("data", data)

# place_market_order: POST /api/v5/trade/order
# body: {"instId": inst_id, "tdMode": "cross", "side": "buy"|"sell", "ordType": "market", "sz": str(contracts)}
# Response has ordId, then fetch fill: GET /api/v5/trade/order?instId=...&ordId=...

# set_leverage: POST /api/v5/account/set-leverage
# body: {"instId": inst_id, "lever": str(leverage), "mgnMode": "cross"}

# get_balance: GET /api/v5/account/balance
# Parse data[0].details[] where ccy == "USDT"

# get_position: GET /api/v5/account/positions?instId={inst_id}

# get_min_order_qty: GET /api/v5/public/instruments?instType=SWAP&instId={inst_id}
# Parse minSz and ctVal
```

### Step 9: Implement trading methods in `src/spread_arb/exchanges/mexc.py`

**Signing approach:** HMAC-SHA256 of request string. API key in header. Similar to Binance.

**IMPORTANT:** MEXC Futures API is at `https://contract.mexc.com`. It uses a DIFFERENT format from spot.

**MEXC Futures specifics:**
- Orders use `vol` (number of contracts, integer)
- `side`: 1=open long, 2=close short, 3=open short, 4=close long  
- `type`: 5=market order
- Contract size varies per symbol — must fetch from `/api/v1/contract/detail`

```python
async def _signed_request(self, method: str, path: str, params: dict | None = None) -> dict:
    ts = str(timestamp_ms())
    params = params or {}
    params["timestamp"] = ts
    
    # Sort params and create query string
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    signature = hmac_sha256_hex(self.api_secret, query)
    
    url = f"https://contract.mexc.com{path}"
    headers = {
        "ApiKey": self.api_key,
        "Request-Time": ts,
        "Signature": signature,
        "Content-Type": "application/json",
    }
    
    if method == "GET":
        url += f"?{query}&Signature={signature}"
        async with self.session.get(url, headers=headers, timeout=...) as resp:
            data = await resp.json()
    else:
        params["Signature"] = signature
        async with self.session.post(url, headers=headers, json=params, timeout=...) as resp:
            data = await resp.json()
    
    if not data.get("success", True):
        raise Exception(f"MEXC API error: {data}")
    return data.get("data", data)

# place_market_order:
# POST /api/v1/private/order/submit
# body: {"symbol": mexc_symbol, "side": 1|3, "type": 5, "vol": int(contracts), "openType": 2 (cross)}
# side 1=open long, 3=open short for entry
# side 2=close short, 4=close long for exit
# IMPORTANT: Caller must specify whether this is an OPEN or CLOSE order.
# Add an `open_side` parameter or determine from context.

# For entry long: side=1 (open long)
# For entry short: side=3 (open short)
# For exit long (sell): side=4 (close long)
# For exit short (buy back): side=2 (close short)

# set_leverage: POST /api/v1/private/position/change_leverage
# get_balance: GET /api/v1/private/account/assets
# get_position: GET /api/v1/private/position/open_positions?symbol={mexc_symbol}
# get_min_order_qty: GET /api/v1/contract/detail?symbol={mexc_symbol} — minVol field
```

**MEXC symbol format:** Use existing `_to_mexc_symbol()` method. MEXC futures uses underscore format like `BTC_USDT`.

### Step 10: Create `src/spread_arb/execution.py` (NEW FILE)

```python
"""Execution service — orchestrates spread entry/exit across two exchanges."""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from .config import Settings
from .exchanges.base import ExchangeClient
from .models import (
    BalanceInfo,
    ExchangeName,
    OrderResult,
    Quote,
    SpreadOrderResult,
)


class ExecutionError(Exception):
    """Raised when order execution fails."""
    pass


class ExecutionService:
    """Stateless executor for spread trades."""

    def __init__(
        self,
        settings: Settings,
        clients: dict[ExchangeName, ExchangeClient],
    ) -> None:
        self.settings = settings
        self.clients = clients
        self.log = logging.getLogger(__name__)
        self._min_qty_cache: dict[tuple[ExchangeName, str], Decimal] = {}

    async def initialize(self, symbols: list[str]) -> None:
        """Set leverage on all exchanges for all symbols. Call once at startup."""
        leverage = self.settings.default_leverage
        for name, client in self.clients.items():
            for symbol in symbols:
                try:
                    await client.set_leverage(symbol, leverage)
                    self.log.info("set leverage %dx on %s for %s", leverage, name.value, symbol)
                except NotImplementedError:
                    self.log.debug("leverage not supported on %s", name.value)
                except Exception as exc:
                    # Some exchanges error if leverage is already set — ignore
                    self.log.warning("set_leverage failed on %s %s: %s", name.value, symbol, exc)

    async def get_balance(self, exchange: ExchangeName) -> BalanceInfo:
        return await self.clients[exchange].get_balance()

    async def execute_spread_entry(
        self,
        symbol: str,
        long_exchange: ExchangeName,
        short_exchange: ExchangeName,
        notional_usdt: float,
        long_quote: Quote,
        short_quote: Quote,
    ) -> SpreadOrderResult:
        """Execute spread entry: buy on long_exchange, sell on short_exchange."""
        # Safety cap
        if notional_usdt > self.settings.max_notional_usdt:
            raise ExecutionError(
                f"notional {notional_usdt} exceeds max {self.settings.max_notional_usdt}"
            )

        long_client = self.clients[long_exchange]
        short_client = self.clients[short_exchange]

        # Calculate quantities
        long_price = float(long_quote.best_ask_price)
        short_price = float(short_quote.best_bid_price)
        long_qty = Decimal(str(notional_usdt)) / Decimal(str(long_price))
        short_qty = Decimal(str(notional_usdt)) / Decimal(str(short_price))

        # Validate minimum order sizes
        await self._validate_min_qty(long_exchange, symbol, long_qty)
        await self._validate_min_qty(short_exchange, symbol, short_qty)

        # Execute both legs concurrently
        try:
            long_result, short_result = await asyncio.wait_for(
                asyncio.gather(
                    long_client.place_market_order(symbol, "buy", long_qty),
                    short_client.place_market_order(symbol, "sell", short_qty),
                    return_exceptions=True,
                ),
                timeout=self.settings.order_timeout_sec,
            )
        except asyncio.TimeoutError:
            raise ExecutionError(f"order timeout after {self.settings.order_timeout_sec}s")

        # Handle partial failures
        if isinstance(long_result, Exception) and isinstance(short_result, Exception):
            raise ExecutionError(f"both legs failed: long={long_result}, short={short_result}")

        if isinstance(long_result, Exception):
            # Short filled but long failed — close short immediately
            self.log.critical("LONG LEG FAILED, closing short | %s | %s", symbol, long_result)
            try:
                await short_client.place_market_order(symbol, "buy", short_result.filled_qty)
            except Exception as close_exc:
                self.log.critical("FAILED TO CLOSE SHORT LEG | %s | %s", symbol, close_exc)
            raise ExecutionError(f"long leg failed: {long_result}")

        if isinstance(short_result, Exception):
            # Long filled but short failed — close long immediately
            self.log.critical("SHORT LEG FAILED, closing long | %s | %s", symbol, short_result)
            try:
                await long_client.place_market_order(symbol, "sell", long_result.filled_qty)
            except Exception as close_exc:
                self.log.critical("FAILED TO CLOSE LONG LEG | %s | %s", symbol, close_exc)
            raise ExecutionError(f"short leg failed: {short_result}")

        self.log.info(
            "spread entry filled | %s | long %s @ %s | short %s @ %s",
            symbol,
            long_exchange.value, long_result.avg_price,
            short_exchange.value, short_result.avg_price,
        )

        return SpreadOrderResult(long_order=long_result, short_order=short_result)

    async def execute_spread_exit(
        self,
        symbol: str,
        long_exchange: ExchangeName,
        short_exchange: ExchangeName,
        long_qty: Decimal,
        short_qty: Decimal,
    ) -> SpreadOrderResult:
        """Execute spread exit: sell long, buy back short."""
        long_client = self.clients[long_exchange]
        short_client = self.clients[short_exchange]

        try:
            long_result, short_result = await asyncio.wait_for(
                asyncio.gather(
                    long_client.place_market_order(symbol, "sell", long_qty),
                    short_client.place_market_order(symbol, "buy", short_qty),
                    return_exceptions=True,
                ),
                timeout=self.settings.order_timeout_sec,
            )
        except asyncio.TimeoutError:
            self.log.critical("EXIT TIMEOUT | %s | manual intervention needed", symbol)
            raise ExecutionError(f"exit timeout after {self.settings.order_timeout_sec}s")

        # For exits, we MUST handle failures — we have open exposure
        if isinstance(long_result, Exception):
            self.log.critical("EXIT LONG LEG FAILED | %s | %s — MANUAL CLOSE NEEDED", symbol, long_result)
        if isinstance(short_result, Exception):
            self.log.critical("EXIT SHORT LEG FAILED | %s | %s — MANUAL CLOSE NEEDED", symbol, short_result)

        if isinstance(long_result, Exception) or isinstance(short_result, Exception):
            raise ExecutionError(f"exit partially failed: long={long_result}, short={short_result}")

        self.log.info(
            "spread exit filled | %s | sell long %s @ %s | buy short %s @ %s",
            symbol,
            long_exchange.value, long_result.avg_price,
            short_exchange.value, short_result.avg_price,
        )

        return SpreadOrderResult(long_order=long_result, short_order=short_result)

    async def _validate_min_qty(
        self, exchange: ExchangeName, symbol: str, qty: Decimal
    ) -> None:
        cache_key = (exchange, symbol)
        if cache_key not in self._min_qty_cache:
            try:
                min_qty = await self.clients[exchange].get_min_order_qty(symbol)
                self._min_qty_cache[cache_key] = min_qty
            except NotImplementedError:
                self._min_qty_cache[cache_key] = Decimal("0")
            except Exception as exc:
                self.log.warning("failed to get min qty for %s %s: %s", exchange.value, symbol, exc)
                self._min_qty_cache[cache_key] = Decimal("0")

        min_qty = self._min_qty_cache[cache_key]
        if qty < min_qty:
            raise ExecutionError(
                f"qty {qty} below minimum {min_qty} on {exchange.value} for {symbol}"
            )
```

### Step 11: Modify `src/spread_arb/mean_reversion_engine.py`

#### 11a. Constructor changes

Add `execution_service` parameter:

```python
def __init__(self, *, settings, opportunity_store, get_latest_quote,
             execution_service=None):  # NEW
    ...existing init...
    self.execution_service = execution_service
    self.live_mode = settings.live_trading and execution_service is not None
    if self.live_mode:
        self.log.info("LIVE TRADING MODE ENABLED — real orders will be placed")
    else:
        self.log.info("Paper trading mode")
```

#### 11b. Modify `_execute_after_delay()` 

After all validation passes and right before creating the `MeanRevPosition` object, add live execution branch:

```python
# === LIVE EXECUTION BRANCH ===
if self.live_mode:
    try:
        spread_result = await self.execution_service.execute_spread_entry(
            symbol=symbol,
            long_exchange=current.long_exchange,
            short_exchange=current.short_exchange,
            notional_usdt=self.settings.mr_notional_usdt,
            long_quote=long_quote,
            short_quote=short_quote,
        )
        # Use ACTUAL fill prices
        entry_long_price = float(spread_result.long_order.avg_price)
        entry_short_price = float(spread_result.short_order.avg_price)
        actual_entry_fees = float(spread_result.long_order.fee + spread_result.short_order.fee)
        actual_qty_long = spread_result.long_order.filled_qty
        actual_qty_short = spread_result.short_order.filled_qty
    except Exception as exc:
        self.log.error("LIVE entry failed | %s | %s", symbol, exc)
        return
else:
    entry_long_price = float(long_quote.best_ask_price)
    entry_short_price = float(short_quote.best_bid_price)
    actual_entry_fees = entry_fees_usdt
    actual_qty_long = None
    actual_qty_short = None
```

Then use these variables when constructing `MeanRevPosition`. Add `actual_qty_long` and `actual_qty_short` fields to `MeanRevPosition` to track actual filled quantities for the exit.

#### 11c. Modify `_close_position()`

Add live execution branch before PnL calculation:

```python
if self.live_mode:
    try:
        spread_result = await self.execution_service.execute_spread_exit(
            symbol=position.symbol,
            long_exchange=position.long_exchange,
            short_exchange=position.short_exchange,
            long_qty=position.actual_qty_long,
            short_qty=position.actual_qty_short,
        )
        exit_long_price = float(spread_result.long_order.avg_price)
        exit_short_price = float(spread_result.short_order.avg_price)
        actual_exit_fees = float(spread_result.long_order.fee + spread_result.short_order.fee)
    except Exception as exc:
        self.log.critical("LIVE exit failed | %s | %s — POSITION STILL OPEN", position.symbol, exc)
        return  # Don't remove from open positions
else:
    exit_long_price = float(long_quote.best_bid_price)
    exit_short_price = float(short_quote.best_ask_price)
```

**NOTE:** `_close_position` is currently NOT async. It needs to become `async def _close_position(...)`. Update the call site in `check_exits()` to use `await`. If `check_exits()` is called synchronously from `_on_quote()`, schedule the close as an `asyncio.create_task()` similar to how `_execute_after_delay` is scheduled.

#### 11d. Add `actual_qty_long` and `actual_qty_short` to `MeanRevPosition`

In the `MeanRevPosition` dataclass (defined in mean_reversion_engine.py or models.py), add:

```python
actual_qty_long: Decimal | None = None   # filled qty from exchange (live only)
actual_qty_short: Decimal | None = None  # filled qty from exchange (live only)
```

### Step 12: Modify `src/spread_arb/scanner.py`

In the `run()` method, after creating `aiohttp.ClientSession`, when creating exchange clients, pass API credentials:

```python
if self.settings.live_trading:
    # Build credential map
    cred_map = {
        ExchangeName.BINANCE: {"api_key": s.api_key_binance, "api_secret": s.api_secret_binance},
        ExchangeName.OKX: {"api_key": s.api_key_okx, "api_secret": s.api_secret_okx, "passphrase": s.api_passphrase_okx},
        ExchangeName.BYBIT: {"api_key": s.api_key_bybit, "api_secret": s.api_secret_bybit},
        ExchangeName.MEXC: {"api_key": s.api_key_mexc, "api_secret": s.api_secret_mexc},
    }
```

Pass credentials when constructing exchange clients. Then create `ExecutionService` and pass to `MeanReversionEngine`.

**IMPORTANT:** The existing exchange client construction in scanner.py creates clients without auth. When `live_trading=True`, construct them WITH credentials from `cred_map`. When `live_trading=False`, construct them as before (no credentials needed).

### Step 13: Create `scripts/test_execution.py` (NEW FILE)

```python
#!/usr/bin/env python3
"""
Test script: opens and immediately closes a small position to verify execution works.

Usage:
    python scripts/test_execution.py --exchange binance --symbol SOLUSDT --notional 10

Requires API keys in .env.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiohttp
from spread_arb.config import get_settings
from spread_arb.models import ExchangeName


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("test_execution")


EXCHANGE_MAP = {
    "binance": ExchangeName.BINANCE,
    "okx": ExchangeName.OKX,
    "bybit": ExchangeName.BYBIT,
    "mexc": ExchangeName.MEXC,
}


def get_client_class(exchange: ExchangeName):
    """Import and return the correct exchange client class."""
    if exchange == ExchangeName.BINANCE:
        from spread_arb.exchanges.binance import BinanceExchange
        return BinanceExchange
    elif exchange == ExchangeName.OKX:
        from spread_arb.exchanges.okx import OkxExchange
        return OkxExchange
    elif exchange == ExchangeName.BYBIT:
        from spread_arb.exchanges.bybit import BybitExchange
        return BybitExchange
    elif exchange == ExchangeName.MEXC:
        from spread_arb.exchanges.mexc import MexcExchange
        return MexcExchange
    else:
        raise ValueError(f"Unsupported exchange: {exchange}")


def get_credentials(settings, exchange: ExchangeName) -> dict:
    """Extract API credentials for the given exchange."""
    if exchange == ExchangeName.BINANCE:
        return {"api_key": settings.api_key_binance, "api_secret": settings.api_secret_binance}
    elif exchange == ExchangeName.OKX:
        return {"api_key": settings.api_key_okx, "api_secret": settings.api_secret_okx, "passphrase": settings.api_passphrase_okx}
    elif exchange == ExchangeName.BYBIT:
        return {"api_key": settings.api_key_bybit, "api_secret": settings.api_secret_bybit}
    elif exchange == ExchangeName.MEXC:
        return {"api_key": settings.api_key_mexc, "api_secret": settings.api_secret_mexc}
    raise ValueError(f"No credentials for {exchange}")


async def run_test(exchange_name: str, symbol: str, notional: float, leverage: int):
    settings = get_settings()
    exchange = EXCHANGE_MAP[exchange_name]
    creds = get_credentials(settings, exchange)
    
    if not creds.get("api_key"):
        log.error("No API key configured for %s. Set API_KEY_%s in .env", exchange_name, exchange_name.upper())
        return

    ClientClass = get_client_class(exchange)
    
    async with aiohttp.ClientSession() as session:
        client = ClientClass(
            session=session,
            request_timeout_sec=settings.request_timeout_sec,
            **creds,
        )
        
        # 1. Check balance
        log.info("=" * 50)
        log.info("Testing %s on %s", symbol, exchange_name.upper())
        log.info("=" * 50)
        
        balance = await client.get_balance()
        log.info("Balance: total=%.2f USDT, available=%.2f USDT", balance.total_usdt, balance.available_usdt)
        
        if float(balance.available_usdt) < notional:
            log.error("Insufficient balance: need %.2f, have %.2f", notional, balance.available_usdt)
            return
        
        # 2. Set leverage
        log.info("Setting leverage to %dx...", leverage)
        await client.set_leverage(symbol, leverage)
        log.info("Leverage set.")
        
        # 3. Get current price
        quote = await client.fetch_quote(symbol)
        ask_price = float(quote.best_ask_price)
        bid_price = float(quote.best_bid_price)
        log.info("Current price: ask=%.4f, bid=%.4f, spread=%.4f%%",
                 ask_price, bid_price, (ask_price - bid_price) / bid_price * 100)
        
        # 4. Calculate quantity
        qty = Decimal(str(notional)) / Decimal(str(ask_price))
        
        # Check minimum
        try:
            min_qty = await client.get_min_order_qty(symbol)
            log.info("Min order qty: %s, our qty: %s", min_qty, qty)
            if qty < min_qty:
                log.error("Qty too small! Need at least %s, have %s. Increase --notional or use a cheaper symbol.", min_qty, qty)
                return
        except NotImplementedError:
            log.warning("Min qty check not available, proceeding...")
        
        # 5. Open position (buy)
        log.info("Opening LONG position: qty=%s (~$%.2f notional)...", qty, notional)
        entry = await client.place_market_order(symbol, "buy", qty)
        log.info("ENTRY FILLED: order_id=%s, qty=%s, avg_price=%s, fee=%s %s",
                 entry.order_id, entry.filled_qty, entry.avg_price, entry.fee, entry.fee_currency)
        
        # 6. Immediately close (sell)
        log.info("Closing position: selling qty=%s...", entry.filled_qty)
        exit_order = await client.place_market_order(symbol, "sell", entry.filled_qty)
        log.info("EXIT FILLED: order_id=%s, qty=%s, avg_price=%s, fee=%s %s",
                 exit_order.order_id, exit_order.filled_qty, exit_order.avg_price,
                 exit_order.fee, exit_order.fee_currency)
        
        # 7. Summary
        entry_cost = float(entry.avg_price) * float(entry.filled_qty)
        exit_revenue = float(exit_order.avg_price) * float(exit_order.filled_qty)
        total_fees = float(entry.fee) + float(exit_order.fee)
        pnl = exit_revenue - entry_cost - total_fees
        
        log.info("=" * 50)
        log.info("ROUND-TRIP SUMMARY")
        log.info("  Entry: %s @ %s = $%.4f", entry.filled_qty, entry.avg_price, entry_cost)
        log.info("  Exit:  %s @ %s = $%.4f", exit_order.filled_qty, exit_order.avg_price, exit_revenue)
        log.info("  Fees:  $%.4f", total_fees)
        log.info("  Net PnL: $%.4f", pnl)
        log.info("  Slippage from mid: %.4f%%",
                 (float(entry.avg_price) - float(exit_order.avg_price)) / float(entry.avg_price) * 100)
        log.info("=" * 50)
        
        # 8. Verify flat
        try:
            pos = await client.get_position(symbol)
            log.info("Current position: size=%s (should be ~0)", pos.size)
        except Exception as exc:
            log.warning("Could not verify position: %s", exc)


def main():
    parser = argparse.ArgumentParser(description="Test exchange order execution")
    parser.add_argument("--exchange", required=True, choices=["binance", "okx", "bybit", "mexc"])
    parser.add_argument("--symbol", default="SOLUSDT", help="Symbol to test (default: SOLUSDT)")
    parser.add_argument("--notional", type=float, default=10.0, help="Notional in USDT (default: 10)")
    parser.add_argument("--leverage", type=int, default=1, help="Leverage (default: 1)")
    args = parser.parse_args()
    
    asyncio.run(run_test(args.exchange, args.symbol, args.notional, args.leverage))


if __name__ == "__main__":
    main()
```

## Verification

After implementation:

1. **Syntax check**: `python -c "from spread_arb.execution import ExecutionService"` should work
2. **Paper mode unchanged**: Run with `LIVE_TRADING=false` (default) and verify behavior is identical to current
3. **Test script**: Run `python scripts/test_execution.py --exchange binance --symbol SOLUSDT --notional 10` — should open and close a $10 SOL position
4. **Check position flat**: After test, verify no open positions remain on the exchange
5. **Check balance impact**: Balance should decrease by only the fees (~$0.01-0.02)

## Important Notes

- **NEVER** set `LIVE_TRADING=true` without API keys — the engine should fail fast with a clear error if `live_trading=True` but keys are empty
- **`max_notional_usdt=50`** is a safety cap — prevents accidentally running with $350 notional on live
- The test script uses SOLUSDT by default because $10 notional on SOL (~$15) gives enough qty to clear minimums. BTC at $100k would give 0.0001 BTC which is likely below minimums.
- MEXC futures has a DIFFERENT API structure from spot — make sure to use `contract.mexc.com` not `api.mexc.com`
- OKX uses contract counts, not base qty — the connector must handle conversion using `ctVal` from instruments endpoint
