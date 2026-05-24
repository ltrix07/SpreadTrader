from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

from ..models import BalanceInfo, ExchangeName, OrderResult, PositionInfo, Quote, Symbol
from .base import ExchangeClient
from .signing import hmac_sha256_hex, timestamp_ms


class BybitExchange(ExchangeClient):
    base_url = "https://api.bybit.com"

    # Canonical symbol -> Bybit-specific symbol.
    _SYMBOL_MAP: dict[str, str] = {
        "PEPEUSDT": "1000PEPEUSDT",
        "FLOKIUSDT": "1000FLOKIUSDT",
        "BONKUSDT": "1000BONKUSDT",
        "SHIBUSDT": "1000SHIBUSDT",
    }

    # When Bybit quotes a "1000X" contract but our canonical symbol is "X",
    # we must divide price by 1000 and multiply size by 1000 to normalise.
    _PRICE_DIVISOR: dict[str, int] = {
        "PEPEUSDT": 1000,
        "FLOKIUSDT": 1000,
        "BONKUSDT": 1000,
        "SHIBUSDT": 1000,
    }

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.BYBIT

    @classmethod
    def _to_bybit_symbol(cls, symbol: Symbol) -> str:
        return cls._SYMBOL_MAP.get(symbol, symbol)

    @classmethod
    def _to_exchange_qty(cls, symbol: str, qty: Decimal) -> Decimal:
        divisor = cls._PRICE_DIVISOR.get(symbol)
        if not divisor:
            return qty
        return qty / Decimal(divisor)

    @classmethod
    def _to_canonical_qty(cls, symbol: str, qty: Decimal) -> Decimal:
        divisor = cls._PRICE_DIVISOR.get(symbol)
        if not divisor:
            return qty
        return qty * Decimal(divisor)

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        bybit_symbol = self._to_bybit_symbol(symbol)
        endpoint = f"{self.base_url}/v5/market/tickers"
        params = {"category": "linear", "symbol": bybit_symbol}

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit API error: {payload}")

        tickers = payload.get("result", {}).get("list", [])
        if not tickers:
            raise RuntimeError(f"Bybit returned no ticker for symbol={symbol}")

        ticker = tickers[0]
        server_ms = payload.get("time")
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = float(received_at_ms - server_ms) if isinstance(server_ms, int) else None

        bid_price = Decimal(ticker["bid1Price"])
        bid_size = Decimal(ticker["bid1Size"])
        ask_price = Decimal(ticker["ask1Price"])
        ask_size = Decimal(ticker["ask1Size"])

        divisor = self._PRICE_DIVISOR.get(symbol)
        if divisor:
            d = Decimal(divisor)
            bid_price /= d
            ask_price /= d
            bid_size *= d
            ask_size *= d

        return Quote(
            received_at=received_at,
            exchange=self.name,
            symbol=symbol,
            best_bid_price=bid_price,
            best_bid_size=bid_size,
            best_ask_price=ask_price,
            best_ask_size=ask_size,
            receive_latency_ms=(time.perf_counter() - started) * 1000.0,
            source_latency_ms=source_latency_ms,
        )

    async def _signed_request(self, method: str, path: str, params: dict | None = None) -> dict:
        ts = str(timestamp_ms())
        recv_window = "5000"
        params = dict(params or {})
        method_upper = method.upper()

        if method_upper == "GET":
            query = urlencode(params)
            sign_payload = f"{ts}{self.api_key}{recv_window}{query}"
            url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"
            body = None
        else:
            body = json.dumps(params)
            sign_payload = f"{ts}{self.api_key}{recv_window}{body}"
            url = f"{self.base_url}{path}"

        signature = hmac_sha256_hex(self.api_secret, sign_payload)
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json",
        }

        async with self.session.request(
            method_upper,
            url,
            headers=headers,
            data=body,
            timeout=self.request_timeout_sec,
        ) as response:
            data = await response.json()

        ret_code = data.get("retCode")
        if ret_code != 0:
            raise RuntimeError(f"Bybit API error: {data}")
        return data.get("result", data)

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        bb_symbol = self._to_bybit_symbol(symbol)
        params = {
            "category": "linear",
            "symbol": bb_symbol,
            "side": "Buy" if side.lower() == "buy" else "Sell",
            "orderType": "Market",
            "qty": str(self._to_exchange_qty(symbol, qty)),
        }
        if close:
            params["reduceOnly"] = True
        data = await self._signed_request("POST", "/v5/order/create", params)
        order_id = data.get("orderId", "")

        # Query final fill details.
        await asyncio.sleep(0.5)
        fills = await self._signed_request(
            "GET",
            "/v5/order/realtime",
            {
                "category": "linear",
                "symbol": bb_symbol,
                "orderId": order_id,
            },
        )
        order_info = (fills.get("list") or [{}])[0]

        filled_qty = self._to_canonical_qty(symbol, Decimal(order_info.get("cumExecQty", "0")))
        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side=side.lower(),
            filled_qty=filled_qty,
            avg_price=Decimal(order_info.get("avgPrice", "0")),
            fee=Decimal(order_info.get("cumExecFee", "0")),
            fee_currency="USDT",
            order_id=order_id,
            timestamp=datetime.now(UTC),
            is_partial=order_info.get("orderStatus") != "Filled",
            raw_response=order_info,
        )

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        stop_price: Decimal,
    ) -> str:
        bb_symbol = self._to_bybit_symbol(symbol)
        side_lower = side.lower()
        params = {
            "category": "linear",
            "symbol": bb_symbol,
            "side": "Buy" if side_lower == "buy" else "Sell",
            "orderType": "Market",
            "qty": str(self._to_exchange_qty(symbol, qty)),
            "triggerPrice": str(stop_price),
            "triggerDirection": 1 if side_lower == "buy" else 2,
        }
        data = await self._signed_request("POST", "/v5/order/create", params)
        return str(data.get("orderId", ""))

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        bb_symbol = self._to_bybit_symbol(symbol)
        await self._signed_request(
            "POST",
            "/v5/order/cancel",
            {
                "category": "linear",
                "symbol": bb_symbol,
                "orderId": order_id,
            },
        )

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        bb_symbol = self._to_bybit_symbol(symbol)
        params = {
            "category": "linear",
            "symbol": bb_symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }
        try:
            await self._signed_request("POST", "/v5/position/set-leverage", params)
        except RuntimeError as exc:
            if "110043" in str(exc):
                return
            raise

    async def get_balance(self) -> BalanceInfo:
        data = await self._signed_request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        accounts = data.get("list") or []
        if not accounts:
            raise RuntimeError("Bybit balance response missing account list")

        account = accounts[0]
        # Account-level available margin (works for both UTA and classic).
        acct_available = account.get("totalAvailableBalance") or "0"

        for coin_info in account.get("coin", []):
            coin_name = coin_info.get("coin") or coin_info.get("coinName")
            if coin_name == "USDT":
                total = coin_info.get("walletBalance") or coin_info.get("equity") or "0"
                # Compute available from equity minus margin in use.
                # Bybit UTA sometimes returns empty strings for available fields.
                equity = Decimal(coin_info.get("equity") or "0")
                order_im = Decimal(coin_info.get("totalOrderIM") or "0")
                position_im = Decimal(coin_info.get("totalPositionIM") or "0")
                available = str(equity - order_im - position_im)
                self.log.info(
                    "bybit balance raw | walletBalance=%s equity=%s bonus=%s locked=%s "
                    "totalOrderIM=%s totalPositionIM=%s availableToWithdraw=%s acct_available=%s",
                    coin_info.get("walletBalance"),
                    coin_info.get("equity"),
                    coin_info.get("bonus"),
                    coin_info.get("locked"),
                    coin_info.get("totalOrderIM"),
                    coin_info.get("totalPositionIM"),
                    coin_info.get("availableToWithdraw"),
                    acct_available,
                )
                return BalanceInfo(
                    exchange=self.name,
                    total_usdt=Decimal(total),
                    available_usdt=Decimal(available),
                )
        raise RuntimeError("USDT balance not found")

    async def get_position(self, symbol: str) -> PositionInfo:
        bb_symbol = self._to_bybit_symbol(symbol)
        data = await self._signed_request(
            "GET",
            "/v5/position/list",
            {"category": "linear", "symbol": bb_symbol},
        )
        positions = data.get("list") or []
        if not positions:
            raise RuntimeError(f"Position not found for {symbol}")

        pos = positions[0]
        size = Decimal(pos.get("size", "0"))
        if str(pos.get("side", "")).lower() == "sell":
            size = -size
        size = self._to_canonical_qty(symbol, size.copy_abs()) * (Decimal("-1") if size < 0 else Decimal("1"))
        return PositionInfo(
            exchange=self.name,
            symbol=symbol,
            size=size,
            entry_price=Decimal(pos.get("avgPrice") or pos.get("entryPrice") or "0"),
            unrealized_pnl=Decimal(pos.get("unrealisedPnl") or pos.get("unrealizedPnl") or "0"),
            leverage=int(Decimal(pos.get("leverage", "1"))),
        )

    async def get_min_order_qty(self, symbol: str) -> Decimal:
        bb_symbol = self._to_bybit_symbol(symbol)
        data = await self._signed_request(
            "GET",
            "/v5/market/instruments-info",
            {"category": "linear", "symbol": bb_symbol},
        )
        instruments = data.get("list") or []
        if not instruments:
            raise RuntimeError(f"Instrument not found for {symbol}")

        min_qty = Decimal(instruments[0]["lotSizeFilter"]["minOrderQty"])
        return self._to_canonical_qty(symbol, min_qty)
