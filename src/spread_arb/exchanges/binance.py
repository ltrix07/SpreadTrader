from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

from ..models import BalanceInfo, ExchangeName, FundingInfo, OrderResult, PositionInfo, Quote, Symbol
from .base import ExchangeClient
from .signing import hmac_sha256_hex, timestamp_ms


class BinanceExchange(ExchangeClient):
    base_url = "https://fapi.binance.com"

    # Binance Futures public rate limit: 2400 request weight / minute.
    # depth?limit=5 costs 2 weight -> 20 symbols x 2 = 40 per cycle.
    # No stagger needed.
    inter_request_delay_sec = 0.0

    # Binance Futures uses 1000-prefixed symbols for low-price tokens.
    _SYMBOL_MAP: dict[str, str] = {
        "PEPEUSDT": "1000PEPEUSDT",
        "FLOKIUSDT": "1000FLOKIUSDT",
        "BONKUSDT": "1000BONKUSDT",
        "SHIBUSDT": "1000SHIBUSDT",
    }

    # When Binance quotes a "1000X" contract but our canonical symbol is "X",
    # we must divide price by 1000 and multiply size by 1000.
    _PRICE_DIVISOR: dict[str, int] = {
        "PEPEUSDT": 1000,
        "FLOKIUSDT": 1000,
        "BONKUSDT": 1000,
        "SHIBUSDT": 1000,
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._lot_size_cache: dict[str, dict[str, Decimal]] = {}

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.BINANCE

    @classmethod
    def _to_binance_symbol(cls, symbol: Symbol) -> str:
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

    async def _lot_size_rules(self, symbol: str) -> dict[str, Decimal]:
        bn_symbol = self._to_binance_symbol(symbol)
        cached = self._lot_size_cache.get(bn_symbol)
        if cached is not None:
            return cached

        url = f"{self.base_url}/fapi/v1/exchangeInfo"
        async with self.session.get(url, timeout=self.request_timeout_sec) as response:
            data = await response.json()
            if response.status != 200:
                raise RuntimeError(f"Binance exchangeInfo error: {data}")
        for symbol_info in data.get("symbols", []):
            if symbol_info.get("symbol") != bn_symbol:
                continue
            filters = symbol_info.get("filters", [])
            market_lot = next((item for item in filters if item.get("filterType") == "MARKET_LOT_SIZE"), None)
            lot_size = next((item for item in filters if item.get("filterType") == "LOT_SIZE"), None)
            selected = market_lot or lot_size
            if selected is None:
                break
            rules = {
                "min_qty": self._to_canonical_qty(symbol, Decimal(selected["minQty"])),
                "step_size": self._to_canonical_qty(symbol, Decimal(selected["stepSize"])),
            }
            self._lot_size_cache[bn_symbol] = rules
            return rules
        raise RuntimeError(f"Lot size rules not found for {symbol}")

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        binance_symbol = self._to_binance_symbol(symbol)
        endpoint = f"{self.base_url}/fapi/v1/depth"
        params = {"symbol": binance_symbol, "limit": "5"}

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        bids = payload.get("bids", [])
        asks = payload.get("asks", [])
        if not bids or not asks:
            raise RuntimeError(f"Binance returned empty book for symbol={binance_symbol}")

        # Binance depth format: [[price, qty], ...]
        best_bid = bids[0]
        best_ask = asks[0]

        server_ms = payload.get("T")  # Transaction time
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = float(received_at_ms - server_ms) if isinstance(server_ms, int) else None

        bid_price = Decimal(best_bid[0])
        bid_size = Decimal(best_bid[1])
        ask_price = Decimal(best_ask[0])
        ask_size = Decimal(best_ask[1])

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

    async def _signed_request(self, method: str, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["timestamp"] = timestamp_ms()
        params["recvWindow"] = 5000
        query = urlencode(params)
        signature = hmac_sha256_hex(self.api_secret, query)
        query = f"{query}&signature={signature}"

        url = f"{self.base_url}{path}?{query}"
        headers = {"X-MBX-APIKEY": self.api_key}

        async with self.session.request(
            method,
            url,
            headers=headers,
            timeout=self.request_timeout_sec,
        ) as response:
            data = await response.json()
            if response.status != 200:
                raise RuntimeError(f"Binance API error: {data}")
            return data

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        bn_symbol = self._to_binance_symbol(symbol)
        params = {
            "symbol": bn_symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": str(self._to_exchange_qty(symbol, qty)),
            "newOrderRespType": "FULL",
        }
        if close:
            params["reduceOnly"] = "true"
        data = await self._signed_request("POST", "/fapi/v1/order", params)
        # FULL response includes 'fills' array with per-fill commission.
        total_fee = Decimal("0")
        for fill in data.get("fills", []):
            total_fee += Decimal(fill.get("commission", "0")).copy_abs()
        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side=side.lower(),
            filled_qty=self._to_canonical_qty(symbol, Decimal(data.get("executedQty", "0"))),
            avg_price=Decimal(data.get("avgPrice", "0")),
            fee=total_fee,
            fee_currency="USDT",
            order_id=str(data.get("orderId", "")),
            timestamp=datetime.fromtimestamp(data.get("updateTime", timestamp_ms()) / 1000, tz=UTC),
            is_partial=data.get("status") != "FILLED",
            raw_response=data,
        )

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        stop_price: Decimal,
    ) -> str:
        bn_symbol = self._to_binance_symbol(symbol)
        params = {
            "symbol": bn_symbol,
            "side": side.upper(),
            "type": "STOP_MARKET",
            "triggerPrice": str(stop_price),
            "quantity": str(self._to_exchange_qty(symbol, qty)),
            "algoType": "CONDITIONAL",
        }
        data = await self._signed_request("POST", "/fapi/v1/algoOrder", params)
        return str(data.get("algoId", data.get("orderId", "")))

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        bn_symbol = self._to_binance_symbol(symbol)
        # Try algo order cancel first (for stop orders), fallback to regular
        try:
            await self._signed_request(
                "DELETE",
                "/fapi/v1/algoOrder",
                {
                    "symbol": bn_symbol,
                    "algoId": order_id,
                },
            )
        except RuntimeError:
            await self._signed_request(
                "DELETE",
                "/fapi/v1/order",
                {
                    "symbol": bn_symbol,
                    "orderId": order_id,
                },
            )

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        bn_symbol = self._to_binance_symbol(symbol)
        await self._signed_request(
            "POST",
            "/fapi/v1/leverage",
            {
                "symbol": bn_symbol,
                "leverage": leverage,
            },
        )

    async def get_balance(self) -> BalanceInfo:
        data = await self._signed_request("GET", "/fapi/v2/balance")
        for item in data:
            if item.get("asset") == "USDT":
                return BalanceInfo(
                    exchange=self.name,
                    total_usdt=Decimal(item["balance"]),
                    available_usdt=Decimal(item["availableBalance"]),
                )
        raise RuntimeError("USDT balance not found")

    async def get_position(self, symbol: str) -> PositionInfo:
        bn_symbol = self._to_binance_symbol(symbol)
        data = await self._signed_request("GET", "/fapi/v2/positionRisk", {"symbol": bn_symbol})
        for item in data:
            if item.get("symbol") == bn_symbol:
                return PositionInfo(
                    exchange=self.name,
                    symbol=symbol,
                    size=self._to_canonical_qty(symbol, Decimal(item.get("positionAmt", "0"))),
                    entry_price=Decimal(item.get("entryPrice", "0")),
                    unrealized_pnl=Decimal(item.get("unRealizedProfit", "0")),
                    leverage=int(item.get("leverage", "1")),
                )
        raise RuntimeError(f"Position not found for {symbol}")

    async def get_min_order_qty(self, symbol: str) -> Decimal:
        return (await self._lot_size_rules(symbol))["min_qty"]

    async def get_qty_step_size(self, symbol: str) -> Decimal:
        return (await self._lot_size_rules(symbol))["step_size"]

    async def get_funding_info(self, symbol: str) -> FundingInfo:
        bn_symbol = self._to_binance_symbol(symbol)
        endpoint = f"{self.base_url}/fapi/v1/premiumIndex"
        async with self.session.get(
            endpoint,
            params={"symbol": bn_symbol},
            timeout=self.request_timeout_sec,
        ) as response:
            response.raise_for_status()
            payload = await response.json()

        next_funding_raw = payload.get("nextFundingTime")
        if next_funding_raw is None:
            raise RuntimeError(f"Binance funding response missing nextFundingTime: {payload}")

        next_funding_time = datetime.fromtimestamp(int(next_funding_raw) / 1000, tz=UTC)
        return FundingInfo(
            exchange=self.name,
            symbol=symbol,
            funding_rate=Decimal(str(payload.get("lastFundingRate", "0"))),
            next_funding_time=next_funding_time,
            funding_interval_hours=8,
            fetched_at=datetime.now(UTC),
        )
