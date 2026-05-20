from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

from ..models import BalanceInfo, ExchangeName, OrderResult, PositionInfo, Quote, Symbol
from .base import ExchangeClient
from .signing import hmac_sha256_base64, timestamp_ms


class BitgetExchange(ExchangeClient):
    base_url = "https://api.bitget.com"

    # Bitget public rate limit: 20 requests / second.
    # With 20 symbols staggered at 50ms -> cycle ~1s, fits comfortably.
    inter_request_delay_sec = 0.05

    # Bitget USDT-M uses 1000-prefixed symbols for low-price tokens,
    # same convention as Bybit/Binance.
    _SYMBOL_MAP: dict[str, str] = {
        "PEPEUSDT": "1000PEPEUSDT",
        "FLOKIUSDT": "1000FLOKIUSDT",
        "BONKUSDT": "1000BONKUSDT",
        "SHIBUSDT": "1000SHIBUSDT",
    }

    _PRICE_DIVISOR: dict[str, int] = {
        "PEPEUSDT": 1000,
        "FLOKIUSDT": 1000,
        "BONKUSDT": 1000,
        "SHIBUSDT": 1000,
    }

    def __init__(
        self,
        session,
        request_timeout_sec: float = 8.0,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        **kwargs: object,
    ) -> None:
        super().__init__(
            session=session,
            request_timeout_sec=request_timeout_sec,
            api_key=api_key,
            api_secret=api_secret,
            **kwargs,
        )
        self.passphrase = passphrase

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.BITGET

    @classmethod
    def _to_bitget_symbol(cls, symbol: Symbol) -> str:
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
        bitget_symbol = self._to_bitget_symbol(symbol)
        endpoint = f"{self.base_url}/api/v2/mix/market/merge-depth"
        params = {
            "productType": "USDT-FUTURES",
            "symbol": bitget_symbol,
            "limit": "1",
        }

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        code = payload.get("code")
        if code != "00000":
            raise RuntimeError(f"Bitget API error: {payload}")

        data = payload.get("data", {})
        asks = data.get("asks", [])
        bids = data.get("bids", [])
        if not bids or not asks:
            raise RuntimeError(f"Bitget returned empty book for symbol={bitget_symbol}")

        # Bitget depth format: [[price, size], ...]
        best_bid = bids[0]
        best_ask = asks[0]

        server_ts = data.get("ts")
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = (
            float(received_at_ms - int(server_ts)) if server_ts else None
        )

        bid_price = Decimal(str(best_bid[0]))
        bid_size = Decimal(str(best_bid[1]))
        ask_price = Decimal(str(best_ask[0]))
        ask_size = Decimal(str(best_ask[1]))

        # Normalise 1000-prefix symbols.
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

    async def _signed_request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Make a signed request to Bitget V2 API."""
        method_upper = method.upper()
        ts = str(timestamp_ms())
        body_str = json.dumps(body) if body else ""
        sign_msg = f"{ts}{method_upper}{path}{body_str}"
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
        if method_upper == "GET":
            async with self.session.get(url, headers=headers, timeout=self.request_timeout_sec) as response:
                data = await response.json()
        else:
            async with self.session.post(
                url,
                headers=headers,
                data=body_str,
                timeout=self.request_timeout_sec,
            ) as response:
                data = await response.json()

        if data.get("code") != "00000":
            raise RuntimeError(f"Bitget API error: {data}")
        return data.get("data", data)

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        bitget_symbol = self._to_bitget_symbol(symbol)
        exchange_qty = self._to_exchange_qty(symbol, qty)
        side_lower = side.lower()
        # One-way mode: no holdSide needed. tradeSide open/close is sufficient.
        body: dict[str, str] = {
            "symbol": bitget_symbol,
            "productType": "USDT-FUTURES",
            "marginMode": "crossed",
            "marginCoin": "USDT",
            "side": side_lower,
            "tradeSide": "close" if close else "open",
            "orderType": "market",
            "size": str(exchange_qty),
        }
        if close:
            body["reduceOnly"] = "YES"
        data = await self._signed_request("POST", "/api/v2/mix/order/place-order", body)
        order_id = str(data.get("orderId", ""))

        await asyncio.sleep(0.5)
        detail_path = "/api/v2/mix/order/detail?" + urlencode({
            "symbol": bitget_symbol,
            "productType": "USDT-FUTURES",
            "orderId": order_id,
        })
        detail = await self._signed_request("GET", detail_path)

        detail_state = str(detail.get("state", "")).lower()
        raw_filled_qty = Decimal(str(detail.get("baseVolume") or detail.get("size") or "0"))
        filled_qty = self._to_canonical_qty(symbol, raw_filled_qty)
        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side=side.lower(),
            filled_qty=filled_qty,
            avg_price=Decimal(str(detail.get("priceAvg") or "0")),
            fee=Decimal(str(detail.get("fee") or "0")).copy_abs(),
            fee_currency="USDT",
            order_id=order_id,
            timestamp=datetime.now(UTC),
            is_partial=detail_state != "filled",
            raw_response=detail,
        )

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        bitget_symbol = self._to_bitget_symbol(symbol)
        try:
            await self._signed_request(
                "POST",
                "/api/v2/mix/account/set-leverage",
                {
                    "symbol": bitget_symbol,
                    "productType": "USDT-FUTURES",
                    "marginCoin": "USDT",
                    "leverage": str(leverage),
                },
            )
        except RuntimeError as exc:
            if "leverage" not in str(exc).lower():
                raise

    async def get_balance(self) -> BalanceInfo:
        data = await self._signed_request("GET", "/api/v2/mix/account/accounts?productType=USDT-FUTURES")
        accounts = data if isinstance(data, list) else data.get("list") or []
        for item in accounts:
            if item.get("marginCoin") == "USDT":
                return BalanceInfo(
                    exchange=self.name,
                    total_usdt=Decimal(str(item.get("usdtEquity", "0"))),
                    available_usdt=Decimal(str(item.get("crossedMaxAvailable") or item.get("available") or "0")),
                )
        raise RuntimeError("USDT balance not found on Bitget")

    async def get_position(self, symbol: str) -> PositionInfo:
        bitget_symbol = self._to_bitget_symbol(symbol)
        data = await self._signed_request(
            "GET",
            "/api/v2/mix/position/single-position?"
            + urlencode({
                "symbol": bitget_symbol,
                "productType": "USDT-FUTURES",
                "marginCoin": "USDT",
            }),
        )

        positions = data if isinstance(data, list) else data.get("list") or []
        if not positions:
            return PositionInfo(
                exchange=self.name,
                symbol=symbol,
                size=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                leverage=1,
            )

        pos = next((item for item in positions if Decimal(str(item.get("total") or "0")) > 0), positions[0])
        size = Decimal(str(pos.get("total") or "0"))
        if str(pos.get("holdSide", "")).lower() == "short":
            size = -size

        size = self._to_canonical_qty(symbol, size.copy_abs()) * (Decimal("-1") if size < 0 else Decimal("1"))
        return PositionInfo(
            exchange=self.name,
            symbol=symbol,
            size=size,
            entry_price=Decimal(str(pos.get("openPriceAvg") or "0")),
            unrealized_pnl=Decimal(str(pos.get("unrealizedPL") or "0")),
            leverage=int(Decimal(str(pos.get("leverage") or "1"))),
        )

    async def get_min_order_qty(self, symbol: str) -> Decimal:
        bitget_symbol = self._to_bitget_symbol(symbol)
        data = await self._signed_request(
            "GET",
            "/api/v2/mix/market/contracts?"
            + urlencode({
                "productType": "USDT-FUTURES",
                "symbol": bitget_symbol,
            }),
        )

        contracts = data if isinstance(data, list) else data.get("list") or []
        if not contracts:
            raise RuntimeError(f"Bitget contract metadata not found for {symbol}")

        min_qty = Decimal(str(contracts[0].get("minTradeNum") or "0"))
        if min_qty <= 0:
            raise RuntimeError(f"Bitget minTradeNum missing for {symbol}")
        return self._to_canonical_qty(symbol, min_qty)

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        stop_price: Decimal,
    ) -> str:
        bitget_symbol = self._to_bitget_symbol(symbol)
        side_lower = side.lower()
        exchange_qty = self._to_exchange_qty(symbol, qty)
        # Round trigger price to 2 decimal places (Bitget rejects excess precision).
        rounded_stop = stop_price.quantize(Decimal("0.01"))
        data = await self._signed_request(
            "POST",
            "/api/v2/mix/order/place-plan-order",
            {
                "planType": "normal_plan",
                "symbol": bitget_symbol,
                "productType": "USDT-FUTURES",
                "marginMode": "crossed",
                "marginCoin": "USDT",
                "side": side_lower,
                "tradeSide": "close",
                "orderType": "market",
                "size": str(exchange_qty),
                "triggerPrice": str(rounded_stop),
                "triggerType": "mark_price",
                "reduceOnly": "YES",
            },
        )
        return str(data.get("orderId", ""))

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        bitget_symbol = self._to_bitget_symbol(symbol)
        await self._signed_request(
            "POST",
            "/api/v2/mix/order/cancel-plan-order",
            {
                "orderId": order_id,
                "symbol": bitget_symbol,
                "productType": "USDT-FUTURES",
                "marginCoin": "USDT",
            },
        )
