from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

from ..models import BalanceInfo, ExchangeName, FundingInfo, OrderResult, PositionInfo, Quote, Symbol
from .base import ExchangeClient
from .signing import hmac_sha256_base64, timestamp_iso


class OkxExchange(ExchangeClient):
    base_url = "https://www.okx.com"

    # OKX public rate limit: 20 requests per 2 seconds.
    # With 20 symbols staggered at 100ms each -> cycle ~2s, fits the limit.
    inter_request_delay_sec = 0.10

    # Canonical symbols that use a "1000" prefix but OKX quotes without it.
    _STRIP_1000_PREFIX: dict[str, str] = {
        "1000BONKUSDT": "BONK",
        "1000SHIBUSDT": "SHIB",
    }

    # For canonical 1000-prefixed symbols, convert price and size accordingly.
    _PRICE_MULTIPLIER: dict[str, int] = {
        "1000BONKUSDT": 1000,
        "1000SHIBUSDT": 1000,
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
        self._instrument_cache: dict[str, dict[str, Decimal]] = {}

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.OKX

    @classmethod
    def _to_okx_inst_id(cls, symbol: Symbol) -> str:
        """Convert canonical symbol (e.g. BTCUSDT) to OKX instId (e.g. BTC-USDT-SWAP)."""
        if symbol in cls._STRIP_1000_PREFIX:
            base = cls._STRIP_1000_PREFIX[symbol]
            return f"{base}-USDT-SWAP"
        if symbol.endswith("USDT") and "_" not in symbol:
            base = symbol.removesuffix("USDT")
            return f"{base}-USDT-SWAP"
        return symbol

    def _canonical_multiplier(self, symbol: str) -> Decimal:
        return Decimal(self._PRICE_MULTIPLIER.get(symbol, 1))

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        inst_id = self._to_okx_inst_id(symbol)
        endpoint = f"{self.base_url}/api/v5/market/books"
        params = {"instId": inst_id, "sz": "1"}

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        code = payload.get("code")
        if code != "0":
            raise RuntimeError(f"OKX API error: {payload}")

        data_list = payload.get("data", [])
        if not data_list:
            raise RuntimeError(f"OKX returned no data for instId={inst_id}")

        book = data_list[0]
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            raise RuntimeError(f"OKX returned empty book for instId={inst_id}")

        # OKX orderbook format: [price, size, deprecated, numOrders]
        best_bid = bids[0]
        best_ask = asks[0]

        server_ts = book.get("ts")
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = (
            float(received_at_ms - int(server_ts)) if server_ts else None
        )

        bid_price = Decimal(str(best_bid[0]))
        bid_size = Decimal(str(best_bid[1]))
        ask_price = Decimal(str(best_ask[0]))
        ask_size = Decimal(str(best_ask[1]))

        multiplier = self._PRICE_MULTIPLIER.get(symbol)
        if multiplier:
            m = Decimal(multiplier)
            bid_price *= m
            ask_price *= m
            bid_size /= m
            ask_size /= m

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

    async def _signed_request(self, method: str, path: str, body: dict | list[dict[str, str]] | None = None) -> dict | list[dict]:
        method_upper = method.upper()
        ts = timestamp_iso()
        body_str = json.dumps(body) if body else ""
        sign_msg = f"{ts}{method_upper}{path}{body_str}"
        signature = hmac_sha256_base64(self.api_secret, sign_msg)

        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

        request_kwargs = {"headers": headers, "timeout": self.request_timeout_sec}
        if body:
            request_kwargs["data"] = body_str

        url = f"{self.base_url}{path}"
        async with self.session.request(method_upper, url, **request_kwargs) as response:
            data = await response.json()
            if data.get("code") != "0":
                raise RuntimeError(f"OKX API error: {data}")
            return data.get("data", data)

    async def _public_get(self, path: str, params: dict[str, str]) -> dict:
        url = f"{self.base_url}{path}"
        async with self.session.get(url, params=params, timeout=self.request_timeout_sec) as response:
            data = await response.json()
            if data.get("code") != "0":
                raise RuntimeError(f"OKX API error: {data}")
            return data.get("data", data)

    async def _instrument_meta(self, symbol: str) -> dict[str, Decimal]:
        inst_id = self._to_okx_inst_id(symbol)
        cached = self._instrument_cache.get(inst_id)
        if cached is not None:
            return cached

        data = await self._public_get(
            "/api/v5/public/instruments",
            {"instType": "SWAP", "instId": inst_id},
        )
        if not data:
            raise RuntimeError(f"OKX instrument not found for {symbol}")
        info = data[0]
        meta = {
            "ctVal": Decimal(info.get("ctVal", "1")),
            "minSz": Decimal(info.get("minSz", "1")),
            "lotSz": Decimal(info.get("lotSz", info.get("minSz", "1"))),
        }
        self._instrument_cache[inst_id] = meta
        return meta

    async def _base_qty_to_contracts(self, symbol: str, qty: Decimal) -> Decimal:
        meta = await self._instrument_meta(symbol)
        ct_val = meta["ctVal"]
        lot_sz = meta["lotSz"]

        underlying_qty = qty * self._canonical_multiplier(symbol)
        contracts = underlying_qty / ct_val
        if lot_sz > 0:
            contracts = (contracts / lot_sz).to_integral_value(rounding=ROUND_DOWN) * lot_sz
        return contracts

    async def _contracts_to_base_qty(self, symbol: str, contracts: Decimal) -> Decimal:
        meta = await self._instrument_meta(symbol)
        ct_val = meta["ctVal"]
        underlying_qty = contracts * ct_val
        return underlying_qty / self._canonical_multiplier(symbol)

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        inst_id = self._to_okx_inst_id(symbol)
        contracts = await self._base_qty_to_contracts(symbol, qty)
        if contracts <= 0:
            raise RuntimeError(f"Computed contract size is zero for {symbol}, qty={qty}")

        order_params = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": side.lower(),
            "ordType": "market",
            "sz": str(contracts),
        }
        if close:
            order_params["reduceOnly"] = "true"
        order_data = await self._signed_request(
            "POST",
            "/api/v5/trade/order",
            order_params,
        )
        placed = order_data[0] if order_data else {}
        ord_id = placed.get("ordId", "")

        path = f"/api/v5/trade/order?{urlencode({'instId': inst_id, 'ordId': ord_id})}"
        details = await self._signed_request("GET", path)
        detail = details[0] if details else {}

        filled_contracts = Decimal(detail.get("accFillSz") or detail.get("fillSz") or "0")
        if filled_contracts == 0:
            filled_contracts = contracts
        filled_qty = await self._contracts_to_base_qty(symbol, filled_contracts)

        fee = Decimal(detail.get("fee", "0")).copy_abs()
        fee_ccy = detail.get("feeCcy") or "USDT"
        avg_price = Decimal(detail.get("avgPx") or detail.get("fillPx") or "0")
        ts_ms = int(detail.get("uTime") or detail.get("cTime") or int(datetime.now(UTC).timestamp() * 1000))
        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side=side.lower(),
            filled_qty=filled_qty,
            avg_price=avg_price,
            fee=fee,
            fee_currency=fee_ccy,
            order_id=ord_id,
            timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
            is_partial=detail.get("state") != "filled",
            raw_response=detail or placed,
        )

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        stop_price: Decimal,
    ) -> str:
        inst_id = self._to_okx_inst_id(symbol)
        contracts = await self._base_qty_to_contracts(symbol, qty)
        if contracts <= 0:
            raise RuntimeError(f"Computed contract size is zero for stop order: {symbol}, qty={qty}")

        data = await self._signed_request(
            "POST",
            "/api/v5/trade/order-algo",
            {
                "instId": inst_id,
                "tdMode": "cross",
                "side": side.lower(),
                "ordType": "trigger",
                "triggerPx": str(stop_price),
                "orderPx": "-1",
                "sz": str(contracts),
                "triggerPxType": "last",
            },
        )
        if isinstance(data, list) and data:
            return str(data[0].get("algoId", ""))
        return ""

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        inst_id = self._to_okx_inst_id(symbol)
        await self._signed_request(
            "POST",
            "/api/v5/trade/cancel-algos",
            [{"instId": inst_id, "algoId": order_id}],
        )

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        inst_id = self._to_okx_inst_id(symbol)
        await self._signed_request(
            "POST",
            "/api/v5/account/set-leverage",
            {"instId": inst_id, "lever": str(leverage), "mgnMode": "cross"},
        )

    async def get_balance(self) -> BalanceInfo:
        data = await self._signed_request("GET", "/api/v5/account/balance")
        if not data:
            raise RuntimeError("OKX balance response empty")

        details = data[0].get("details", [])
        for item in details:
            if item.get("ccy") == "USDT":
                return BalanceInfo(
                    exchange=self.name,
                    total_usdt=Decimal(item.get("eq", "0")),
                    available_usdt=Decimal(item.get("availEq", "0")),
                )
        raise RuntimeError("USDT balance not found")

    async def get_position(self, symbol: str) -> PositionInfo:
        inst_id = self._to_okx_inst_id(symbol)
        path = f"/api/v5/account/positions?{urlencode({'instId': inst_id})}"
        data = await self._signed_request("GET", path)
        if not data:
            return PositionInfo(
                exchange=self.name,
                symbol=symbol,
                size=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                leverage=1,
            )

        pos = data[0]
        contract_pos = Decimal(pos.get("pos", "0"))
        size = await self._contracts_to_base_qty(symbol, contract_pos.copy_abs())
        if contract_pos < 0:
            size = -size
        return PositionInfo(
            exchange=self.name,
            symbol=symbol,
            size=size,
            entry_price=Decimal(pos.get("avgPx") or pos.get("nonSettleAvgPx") or "0"),
            unrealized_pnl=Decimal(pos.get("upl", "0")),
            leverage=int(Decimal(pos.get("lever", "1"))),
        )

    async def get_min_order_qty(self, symbol: str) -> Decimal:
        meta = await self._instrument_meta(symbol)
        min_contracts = meta["minSz"]
        return await self._contracts_to_base_qty(symbol, min_contracts)

    async def get_funding_info(self, symbol: str) -> FundingInfo:
        inst_id = self._to_okx_inst_id(symbol)
        endpoint = f"{self.base_url}/api/v5/public/funding-rate"
        async with self.session.get(
            endpoint,
            params={"instId": inst_id},
            timeout=self.request_timeout_sec,
        ) as response:
            response.raise_for_status()
            payload = await response.json()

        if payload.get("code") != "0":
            raise RuntimeError(f"OKX funding API error: {payload}")
        data = payload.get("data", [])
        if not data:
            raise RuntimeError(f"OKX funding response missing data for {symbol}")
        item = data[0]

        next_raw = item.get("nextFundingTime")
        if next_raw is None:
            raise RuntimeError(f"OKX funding response missing nextFundingTime: {item}")
        next_ms = int(str(next_raw))
        next_funding_time = datetime.fromtimestamp(next_ms / 1000, tz=UTC)

        interval_hours = 8
        funding_time_raw = item.get("fundingTime")
        if funding_time_raw is not None:
            funding_ms = int(str(funding_time_raw))
            diff_ms = next_ms - funding_ms
            if diff_ms > 0:
                interval_hours = max(1, int(round(diff_ms / 3_600_000)))

        return FundingInfo(
            exchange=self.name,
            symbol=symbol,
            funding_rate=Decimal(str(item.get("fundingRate", "0"))),
            next_funding_time=next_funding_time,
            funding_interval_hours=interval_hours,
            fetched_at=datetime.now(UTC),
        )
