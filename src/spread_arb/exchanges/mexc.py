from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
from urllib.parse import quote, urlencode

from aiohttp import ContentTypeError

from ..models import BalanceInfo, ExchangeName, OrderResult, PositionInfo, Quote, Symbol
from .base import ExchangeClient
from .signing import hmac_sha256_hex, timestamp_ms


class MexcExchange(ExchangeClient):
    base_url = "https://contract.mexc.com"
    inter_request_delay_sec = 0.12  # ~120ms between requests to stay under MEXC rate limit.

    def __init__(
        self,
        session,
        request_timeout_sec: float = 8.0,
        api_key: str = "",
        api_secret: str = "",
        **kwargs: object,
    ) -> None:
        super().__init__(
            session=session,
            request_timeout_sec=request_timeout_sec,
            api_key=api_key,
            api_secret=api_secret,
            **kwargs,
        )
        self._contract_cache: dict[str, dict] = {}

    @property
    def name(self) -> ExchangeName:
        return ExchangeName.MEXC

    @staticmethod
    def _to_mexc_symbol(symbol: Symbol) -> str:
        if symbol.endswith("USDT") and "_" not in symbol:
            base = symbol.removesuffix("USDT")
            return f"{base}_USDT"
        return symbol

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        mexc_symbol = self._to_mexc_symbol(symbol)
        endpoint = f"{self.base_url}/api/v1/contract/depth/{mexc_symbol}"
        params = {"limit": 1}

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        if not payload.get("success"):
            raise RuntimeError(f"MEXC API error: {payload}")

        data = payload.get("data") or {}
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            raise RuntimeError(f"MEXC returned empty depth for symbol={symbol}")

        best_bid = bids[0]
        best_ask = asks[0]

        server_ms = data.get("timestamp")
        received_at = datetime.now(UTC)
        received_at_ms = int(received_at.timestamp() * 1000)
        source_latency_ms = float(received_at_ms - server_ms) if isinstance(server_ms, int) else None

        return Quote(
            received_at=received_at,
            exchange=self.name,
            symbol=symbol,
            best_bid_price=Decimal(str(best_bid[0])),
            best_bid_size=Decimal(str(best_bid[1])),
            best_ask_price=Decimal(str(best_ask[0])),
            best_ask_size=Decimal(str(best_ask[1])),
            receive_latency_ms=(time.perf_counter() - started) * 1000.0,
            source_latency_ms=source_latency_ms,
        )

    async def _signed_request(self, method: str, path: str, params: dict | None = None) -> dict:
        ts = str(timestamp_ms())
        req_params = {
            key: value
            for key, value in dict(params or {}).items()
            if value is not None
        }
        method_upper = method.upper()

        if method_upper in {"GET", "DELETE"}:
            # MEXC contract signature string for GET/DELETE:
            # sorted URL-encoded service params joined by '&'.
            request_param_str = urlencode(
                sorted(req_params.items()),
                quote_via=quote,
            )
            request_body_str = ""
        else:
            # MEXC contract signature string for POST:
            # JSON body string as-is (no key sorting required).
            # IMPORTANT: the signed string and the request body must be identical.
            request_body_str = json.dumps(req_params, separators=(",", ":"), ensure_ascii=False)
            request_param_str = request_body_str

        sign_payload = f"{self.api_key}{ts}{request_param_str}"
        signature = hmac_sha256_hex(self.api_secret, sign_payload)

        url = f"{self.base_url}{path}"
        headers = {
            "ApiKey": self.api_key,
            "Request-Time": ts,
            "Signature": signature,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "python-requests/2.32.3",
            "source": "CCXT",
        }

        if method_upper == "GET":
            req_url = f"{url}?{request_param_str}" if request_param_str else url
            request_kwargs: dict[str, object] = {}
        elif method_upper == "DELETE":
            req_url = f"{url}?{request_param_str}" if request_param_str else url
            request_kwargs = {}
        else:
            req_url = url
            request_kwargs = {"data": request_body_str}

        async with self.session.request(
            method_upper,
            req_url,
            headers=headers,
            timeout=self.request_timeout_sec,
            **request_kwargs,
        ) as response:
            try:
                data = await response.json(content_type=None)
            except (ContentTypeError, json.JSONDecodeError):
                text = await response.text()
                snippet = " ".join(text.strip().split())[:300]
                raise RuntimeError(
                    f"MEXC HTTP {response.status} non-JSON response at {path}: {snippet}"
                ) from None

        if not data.get("success", True):
            raise RuntimeError(f"MEXC API error: {data}")
        if data.get("code") not in (None, 0):
            raise RuntimeError(f"MEXC API error: {data}")
        return data.get("data", data)

    async def _contract_detail(self, symbol: str) -> dict:
        mexc_symbol = self._to_mexc_symbol(symbol)
        cached = self._contract_cache.get(mexc_symbol)
        if cached is not None:
            return cached

        url = f"{self.base_url}/api/v1/contract/detail"
        async with self.session.get(url, params={"symbol": mexc_symbol}, timeout=self.request_timeout_sec) as response:
            data = await response.json()
            if not data.get("success", True):
                raise RuntimeError(f"MEXC contract detail error: {data}")
            details = data.get("data") or []
            if isinstance(details, dict):
                detail = details
            else:
                detail = next((item for item in details if item.get("symbol") == mexc_symbol), None)
            if detail is None:
                raise RuntimeError(f"MEXC contract metadata not found for {symbol}")
            self._contract_cache[mexc_symbol] = detail
            return detail

    async def _base_qty_to_contracts(self, symbol: str, qty: Decimal) -> int:
        detail = await self._contract_detail(symbol)
        contract_size = Decimal(str(detail.get("contractSize", "1")))
        contracts = (qty / contract_size).to_integral_value(rounding=ROUND_DOWN)
        return int(contracts)

    async def _contracts_to_base_qty(self, symbol: str, contracts: Decimal) -> Decimal:
        detail = await self._contract_detail(symbol)
        contract_size = Decimal(str(detail.get("contractSize", "1")))
        return contracts * contract_size

    async def _mexc_order_side(self, symbol: str, side: str, close: bool = False) -> int:
        # MEXC side:
        # 1=open long, 2=close short, 3=open short, 4=close long.
        # Prefer explicit close intent; fallback to position inference.
        side_lower = side.lower()
        if close:
            if side_lower == "buy":
                return 2
            if side_lower == "sell":
                return 4
            raise ValueError(f"Unsupported side: {side}")

        position = await self.get_position(symbol)
        if side_lower == "buy":
            return 2 if position.size < 0 else 1
        if side_lower == "sell":
            return 4 if position.size > 0 else 3
        raise ValueError(f"Unsupported side: {side}")

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        mexc_symbol = self._to_mexc_symbol(symbol)
        contracts = await self._base_qty_to_contracts(symbol, qty)
        if contracts <= 0:
            raise RuntimeError(f"Computed contract volume is zero for {symbol}, qty={qty}")

        mexc_side = await self._mexc_order_side(symbol, side, close=close)
        data = await self._signed_request(
            "POST",
            "/api/v1/private/order/submit",
            {
                "symbol": mexc_symbol,
                "side": mexc_side,
                "type": 5,
                "vol": contracts,
                "openType": 2,
            },
        )
        order_id = str(data.get("orderId", ""))
        detail = {}
        if order_id:
            try:
                detail = await self._signed_request("GET", f"/api/v1/private/order/get/{order_id}")
            except Exception:
                detail = {}

        filled_contracts = Decimal(
            str(
                detail.get("dealVol")
                or detail.get("vol")
                or contracts
            )
        )
        avg_price = Decimal(str(detail.get("avgPrice") or detail.get("price") or "0"))
        fee = Decimal(str(detail.get("fee") or detail.get("takerFee") or "0")).copy_abs()
        update_time = detail.get("updateTime") or detail.get("createTime") or timestamp_ms()
        status = str(detail.get("state") or detail.get("status") or "")
        filled_qty = await self._contracts_to_base_qty(symbol, filled_contracts)
        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side=side.lower(),
            filled_qty=filled_qty,
            avg_price=avg_price,
            fee=fee,
            fee_currency="USDT",
            order_id=order_id,
            timestamp=datetime.fromtimestamp(int(update_time) / 1000, tz=UTC),
            is_partial=status not in {"4", "5", "filled", "FILLED"},
            raw_response=detail or data,
        )

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        stop_price: Decimal,
    ) -> str:
        mexc_symbol = self._to_mexc_symbol(symbol)
        contracts = await self._base_qty_to_contracts(symbol, qty)
        if contracts <= 0:
            raise RuntimeError(f"Computed contract volume is zero for stop order: {symbol}, qty={qty}")

        side_lower = side.lower()
        mexc_side = 4 if side_lower == "sell" else 2
        data = await self._signed_request(
            "POST",
            "/api/v1/private/planorder/place",
            {
                "symbol": mexc_symbol,
                "side": mexc_side,
                "type": 5,
                "triggerPrice": str(stop_price),
                "triggerType": 1,
                "vol": contracts,
                "openType": 2,
            },
        )
        order_id = data.get("orderId") or data.get("id") or data.get("data")
        return str(order_id or "")

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        mexc_symbol = self._to_mexc_symbol(symbol)
        await self._signed_request(
            "POST",
            "/api/v1/private/planorder/cancel",
            {
                "symbol": mexc_symbol,
                "orderId": order_id,
            },
        )

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        mexc_symbol = self._to_mexc_symbol(symbol)
        # Prefer positionId when there is an active position. MEXC validates
        # change_leverage differently with and without positions.
        try:
            raw_positions = await self._signed_request(
                "GET",
                "/api/v1/private/position/open_positions",
                {"symbol": mexc_symbol},
            )
        except RuntimeError:
            raw_positions = []

        positions = (
            raw_positions
            if isinstance(raw_positions, list)
            else raw_positions.get("rows") or raw_positions.get("list") or []
        )
        active_pos = next(
            (
                item
                for item in positions
                if Decimal(str(item.get("holdVol") or item.get("positionVol") or item.get("vol") or "0")) > 0
            ),
            None,
        )

        if active_pos:
            position_id = active_pos.get("positionId") or active_pos.get("id")
            if position_id is not None:
                await self._signed_request(
                    "POST",
                    "/api/v1/private/position/change_leverage",
                    {"positionId": int(position_id), "leverage": leverage},
                )
                return

        # No active position: docs require symbol + openType + positionType.
        # Try both position sides and both margin modes because account config
        # may differ between symbols.
        attempts = [
            {"symbol": mexc_symbol, "leverage": leverage, "openType": 2, "positionType": 1},
            {"symbol": mexc_symbol, "leverage": leverage, "openType": 2, "positionType": 2},
            {"symbol": mexc_symbol, "leverage": leverage, "openType": 1, "positionType": 1},
            {"symbol": mexc_symbol, "leverage": leverage, "openType": 1, "positionType": 2},
        ]
        last_error: RuntimeError | None = None
        for payload in attempts:
            try:
                await self._signed_request(
                    "POST",
                    "/api/v1/private/position/change_leverage",
                    payload,
                )
                return
            except RuntimeError as exc:
                last_error = exc

        if last_error is not None:
            raise last_error

    async def get_balance(self) -> BalanceInfo:
        data = await self._signed_request("GET", "/api/v1/private/account/assets")
        assets = data if isinstance(data, list) else data.get("assets", [])
        for item in assets:
            if item.get("currency") == "USDT":
                total = item.get("balance") or item.get("equity") or "0"
                available = item.get("availableBalance") or item.get("available") or "0"
                return BalanceInfo(
                    exchange=self.name,
                    total_usdt=Decimal(str(total)),
                    available_usdt=Decimal(str(available)),
                )
        raise RuntimeError("USDT balance not found")

    async def get_position(self, symbol: str) -> PositionInfo:
        mexc_symbol = self._to_mexc_symbol(symbol)
        data = await self._signed_request(
            "GET",
            "/api/v1/private/position/open_positions",
            {"symbol": mexc_symbol},
        )
        positions = data if isinstance(data, list) else data.get("rows") or data.get("list") or []
        if not positions:
            return PositionInfo(
                exchange=self.name,
                symbol=symbol,
                size=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                leverage=1,
            )

        pos = positions[0]
        vol = Decimal(str(pos.get("holdVol") or pos.get("positionVol") or pos.get("vol") or "0"))
        side = int(pos.get("positionType") or pos.get("positionSide") or 1)
        size = await self._contracts_to_base_qty(symbol, vol)
        if side in (2, 3, 4):
            size = -size
        return PositionInfo(
            exchange=self.name,
            symbol=symbol,
            size=size,
            entry_price=Decimal(str(pos.get("openAvgPrice") or pos.get("openPrice") or "0")),
            unrealized_pnl=Decimal(str(pos.get("unrealizedPnl") or pos.get("unrealisedPnl") or "0")),
            leverage=int(Decimal(str(pos.get("leverage") or "1"))),
        )

    async def get_min_order_qty(self, symbol: str) -> Decimal:
        detail = await self._contract_detail(symbol)
        min_vol = Decimal(str(detail.get("minVol", "1")))
        return await self._contracts_to_base_qty(symbol, min_vol)
