from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

from ..models import BalanceInfo, ExchangeName, OrderResult, PositionInfo, Quote, Symbol
from .base import ExchangeClient
from .signing import hmac_sha512_hex, sha512_hex


class GateExchange(ExchangeClient):
    base_url = "https://api.gateio.ws"

    # Gate.io public rate limit: 900 requests / minute (15 req/s).
    # With 20 symbols staggered at 70ms → cycle ~1.4s, well within limits.
    inter_request_delay_sec = 0.07

    # Gate.io uses underscore-separated contract names: BTC_USDT.
    # For low-price tokens, Gate uses the raw name (no 1000 prefix).
    _STRIP_1000_PREFIX: dict[str, str] = {
        "1000BONKUSDT": "BONK_USDT",
    }

    # When Gate quotes per-unit but canonical is per-1000-units,
    # multiply price by this factor.
    _PRICE_MULTIPLIER: dict[str, int] = {
        "1000BONKUSDT": 1000,
    }

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
        return ExchangeName.GATE

    @classmethod
    def _to_gate_contract(cls, symbol: Symbol) -> str:
        """Convert canonical symbol (e.g. BTCUSDT) to Gate contract (e.g. BTC_USDT)."""
        if symbol in cls._STRIP_1000_PREFIX:
            return cls._STRIP_1000_PREFIX[symbol]
        if symbol.endswith("USDT") and "_" not in symbol:
            base = symbol.removesuffix("USDT")
            return f"{base}_USDT"
        return symbol

    async def fetch_quote(self, symbol: Symbol) -> Quote:
        started = time.perf_counter()
        contract = self._to_gate_contract(symbol)
        endpoint = f"{self.base_url}/api/v4/futures/usdt/order_book"
        params = {"contract": contract, "limit": "1", "with_id": "true"}

        async with self.session.get(endpoint, params=params, timeout=self.request_timeout_sec) as response:
            response.raise_for_status()
            payload = await response.json()

        # Gate returns {"asks": [{"p":"...", "s":...}], "bids": [...], "update_id":...}
        asks = payload.get("asks", [])
        bids = payload.get("bids", [])
        if not bids or not asks:
            raise RuntimeError(f"Gate returned empty book for contract={contract}")

        best_bid = bids[0]
        best_ask = asks[0]

        received_at = datetime.now(UTC)

        bid_price = Decimal(str(best_bid["p"]))
        bid_size = Decimal(str(best_bid["s"]))
        ask_price = Decimal(str(best_ask["p"]))
        ask_size = Decimal(str(best_ask["s"]))

        # Gate sizes are in contracts; for most USDT pairs 1 contract = 1 unit.
        # Normalise 1000-prefix symbols.
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
            source_latency_ms=None,  # Gate doesn't include server timestamp in orderbook.
        )

    @classmethod
    def _canonical_multiplier(cls, symbol: str) -> Decimal:
        return Decimal(cls._PRICE_MULTIPLIER.get(symbol, 1))

    @staticmethod
    def _extract_order_id(payload: object) -> str:
        if isinstance(payload, dict):
            return str(payload.get("id") or payload.get("order_id") or payload.get("orderId") or "")
        if isinstance(payload, (str, int)):
            return str(payload)
        return ""

    @staticmethod
    def _extract_avg_price(detail: dict[str, object]) -> Decimal:
        for field in ("avg_deal_price", "avg_price", "fill_price", "price"):
            raw = detail.get(field)
            if raw is None:
                continue
            value = Decimal(str(raw))
            if value > 0:
                return value
        return Decimal("0")

    @staticmethod
    def _extract_filled_contracts(detail: dict[str, object], fallback: Decimal) -> Decimal:
        size = Decimal(str(detail.get("size") or "0"))
        left = Decimal(str(detail.get("left") or "0"))
        if size != 0:
            filled = size.copy_abs() - left.copy_abs()
            if filled > 0:
                return filled

        for field in ("filled_amount", "filled_size"):
            raw = detail.get(field)
            if raw is not None:
                value = Decimal(str(raw)).copy_abs()
                if value > 0:
                    return value
        return fallback.copy_abs()

    @staticmethod
    def _timestamp_to_datetime(value: object) -> datetime:
        if value is None:
            return datetime.now(UTC)
        ts = float(value)
        # Accept both seconds and milliseconds.
        if ts > 10_000_000_000:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)

    @staticmethod
    def _is_not_found_error(exc: RuntimeError) -> bool:
        message = str(exc).upper()
        return "404" in message or "POSITION_NOT_FOUND" in message or "ORDER_NOT_FOUND" in message

    async def _signed_request(
        self,
        method: str,
        path: str,
        query_params: dict | None = None,
        body: dict | None = None,
    ) -> dict:
        method_upper = method.upper()
        params = {
            key: value
            for key, value in dict(query_params or {}).items()
            if value is not None
        }
        query_string = urlencode(params)
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
        payload_hash = sha512_hex(body_str)
        ts = str(int(time.time()))
        sign_payload = f"{method_upper}\n{path}\n{query_string}\n{payload_hash}\n{ts}"
        signature = hmac_sha512_hex(self.api_secret, sign_payload)

        headers = {
            "KEY": self.api_key,
            "SIGN": signature,
            "Timestamp": ts,
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}{path}"
        request_kwargs: dict[str, object] = {
            "headers": headers,
            "timeout": self.request_timeout_sec,
        }
        if params:
            request_kwargs["params"] = params
        if method_upper in {"POST", "PUT", "DELETE"}:
            request_kwargs["data"] = body_str

        async with self.session.request(method_upper, url, **request_kwargs) as response:
            raw_text = await response.text()

        try:
            data = json.loads(raw_text) if raw_text else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gate HTTP {response.status} non-JSON response at {path}: {raw_text[:300]}") from exc

        if response.status >= 400 or (isinstance(data, dict) and data.get("label")):
            self.log.error(
                "Gate request failed | method=%s path=%s status=%s response=%s",
                method_upper,
                path,
                response.status,
                data,
            )
            if isinstance(data, dict):
                label = data.get("label")
                message = data.get("message")
                if label:
                    raise RuntimeError(f"Gate API error [{label}] ({response.status}): {message}")
            raise RuntimeError(f"Gate API error ({response.status}): {data}")

        if isinstance(data, dict):
            return data
        raise RuntimeError(f"Unexpected Gate response type at {path}: {data}")

    async def _contract_detail(self, symbol: str) -> dict:
        contract = self._to_gate_contract(symbol)
        cached = self._contract_cache.get(contract)
        if cached is not None:
            return cached

        detail = await self._signed_request("GET", f"/api/v4/futures/usdt/contracts/{contract}")
        if not detail:
            raise RuntimeError(f"Gate contract metadata not found for {symbol}")
        self._contract_cache[contract] = detail
        return detail

    async def _base_qty_to_contracts(self, symbol: str, qty: Decimal) -> int:
        detail = await self._contract_detail(symbol)
        multiplier = Decimal(str(detail.get("quanto_multiplier") or "0"))
        if multiplier <= 0:
            raise RuntimeError(f"Gate quanto_multiplier missing for {symbol}: {detail}")

        underlying_qty = qty * self._canonical_multiplier(symbol)
        contracts = (underlying_qty / multiplier).to_integral_value(rounding=ROUND_DOWN)
        return int(contracts)

    async def _contracts_to_base_qty(self, symbol: str, contracts: Decimal) -> Decimal:
        detail = await self._contract_detail(symbol)
        multiplier = Decimal(str(detail.get("quanto_multiplier") or "0"))
        if multiplier <= 0:
            raise RuntimeError(f"Gate quanto_multiplier missing for {symbol}: {detail}")

        underlying_qty = contracts * multiplier
        return underlying_qty / self._canonical_multiplier(symbol)

    async def _fetch_order_detail_with_retry(
        self,
        order_id: str,
        max_attempts: int = 3,
        delay_sec: float = 0.5,
    ) -> dict[str, object]:
        detail: dict[str, object] = {}
        if not order_id:
            return detail

        path = f"/api/v4/futures/usdt/orders/{order_id}"
        for attempt in range(1, max_attempts + 1):
            await asyncio.sleep(delay_sec)
            try:
                raw = await self._signed_request("GET", path)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Gate order detail fetch attempt %d failed: %s", attempt, exc)
                continue

            detail = raw if isinstance(raw, dict) else {}
            if self._extract_avg_price(detail) > 0:
                return detail
            self.log.info(
                "Gate order detail attempt %d/%d: avg price still 0, retrying...",
                attempt,
                max_attempts,
            )

        return detail

    async def get_balance(self) -> BalanceInfo:
        data = await self._signed_request("GET", "/api/v4/futures/usdt/accounts")
        return BalanceInfo(
            exchange=self.name,
            total_usdt=Decimal(str(data.get("total") or "0")),
            available_usdt=Decimal(str(data.get("available") or "0")),
        )

    async def get_position(self, symbol: str) -> PositionInfo:
        contract = self._to_gate_contract(symbol)
        try:
            data = await self._signed_request("GET", f"/api/v4/futures/usdt/positions/{contract}")
        except RuntimeError as exc:
            if self._is_not_found_error(exc):
                data = {}
            else:
                raise

        raw_size = Decimal(str(data.get("size") or "0"))
        if raw_size == 0:
            return PositionInfo(
                exchange=self.name,
                symbol=symbol,
                size=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                leverage=1,
            )

        size = await self._contracts_to_base_qty(symbol, raw_size.copy_abs())
        if raw_size < 0:
            size = -size
        return PositionInfo(
            exchange=self.name,
            symbol=symbol,
            size=size,
            entry_price=Decimal(str(data.get("entry_price") or "0")),
            unrealized_pnl=Decimal(str(data.get("unrealised_pnl") or "0")),
            leverage=int(Decimal(str(data.get("leverage") or "1"))),
        )

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        contract = self._to_gate_contract(symbol)
        path = f"/api/v4/futures/usdt/positions/{contract}/leverage"
        # Gate docs: POST with query param `leverage` (not JSON body).
        query = {"leverage": str(leverage)}
        try:
            await self._signed_request("POST", path, query_params=query)
            return
        except RuntimeError as exc:
            message = str(exc).lower()
            if "leverage" in message:
                return
            raise

    async def get_min_order_qty(self, symbol: str) -> Decimal:
        detail = await self._contract_detail(symbol)
        min_contracts = Decimal(str(detail.get("order_size_min") or "0"))
        if min_contracts <= 0:
            min_contracts = Decimal("1")
        return await self._contracts_to_base_qty(symbol, min_contracts)

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        contract = self._to_gate_contract(symbol)
        contracts = await self._base_qty_to_contracts(symbol, qty)
        if contracts <= 0:
            raise RuntimeError(f"Computed contract size is zero for {symbol}, qty={qty}")

        side_lower = side.lower()
        signed_size = contracts if side_lower == "buy" else -contracts
        request_body: dict[str, object] = {
            "contract": contract,
            "size": signed_size,
            "price": "0",
            "tif": "ioc",
        }
        if close:
            request_body["close"] = True
            request_body["reduce_only"] = True

        placed = await self._signed_request("POST", "/api/v4/futures/usdt/orders", body=request_body)
        order_id = self._extract_order_id(placed)
        if not order_id:
            raise RuntimeError(f"Gate order placement response missing order id: {placed}")

        detail = await self._fetch_order_detail_with_retry(order_id)
        filled_contracts = self._extract_filled_contracts(detail, Decimal(contracts))
        avg_price = self._extract_avg_price(detail)
        if avg_price <= 0:
            self.log.error(
                "Gate avg_price=0 after retries | symbol=%s side=%s order_id=%s detail=%s",
                symbol,
                side,
                order_id,
                detail,
            )

        return OrderResult(
            exchange=self.name,
            symbol=symbol,
            side=side_lower,
            filled_qty=await self._contracts_to_base_qty(symbol, filled_contracts),
            avg_price=avg_price,
            fee=Decimal(str(detail.get("fee") or "0")).copy_abs(),
            fee_currency=str(detail.get("fee_currency") or "USDT"),
            order_id=order_id,
            timestamp=self._timestamp_to_datetime(
                detail.get("finish_time") or detail.get("update_time") or detail.get("create_time"),
            ),
            is_partial=str(detail.get("finish_as") or "").lower() != "filled",
            raw_response=detail or placed,
        )

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        stop_price: Decimal,
    ) -> str:
        contract = self._to_gate_contract(symbol)
        contracts = await self._base_qty_to_contracts(symbol, qty)
        if contracts <= 0:
            raise RuntimeError(f"Computed contract size is zero for stop order: {symbol}, qty={qty}")

        side_lower = side.lower()
        signed_size = contracts if side_lower == "buy" else -contracts
        # Gate trigger rule: 1 means trigger when price >= trigger, 2 means <= trigger.
        trigger_rule = 1 if side_lower == "buy" else 2
        payload = {
            "initial": {
                "contract": contract,
                "size": signed_size,
                "price": "0",
                "tif": "ioc",
                "close": True,
                "reduce_only": True,
            },
            "trigger": {
                "strategy_type": 0,
                "price_type": 1,  # mark price
                "price": str(stop_price),
                "rule": trigger_rule,
                "expiration": 86400,
            },
        }
        data = await self._signed_request("POST", "/api/v4/futures/usdt/price_orders", body=payload)
        return self._extract_order_id(data)

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        _ = symbol
        try:
            await self._signed_request("DELETE", f"/api/v4/futures/usdt/price_orders/{order_id}")
        except RuntimeError as exc:
            if self._is_not_found_error(exc):
                return
            raise
