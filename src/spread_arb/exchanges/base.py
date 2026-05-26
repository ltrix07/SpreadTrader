from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal

from aiohttp import ClientSession

from ..models import BalanceInfo, ExchangeName, FundingInfo, OrderResult, PositionInfo, Quote, Symbol

QuoteCallback = Callable[[Quote], Awaitable[None] | None]


class ExchangeClient(ABC):
    # Delay between launching each request in a poll cycle (seconds).
    # Staggers requests to avoid burst rate-limit hits.
    # Override in subclasses with stricter rate limits (e.g. MEXC).
    inter_request_delay_sec: float = 0.0

    def __init__(
        self,
        session: ClientSession,
        request_timeout_sec: float = 8.0,
        api_key: str = "",
        api_secret: str = "",
        **_: object,
    ) -> None:
        self.session = session
        self.request_timeout_sec = request_timeout_sec
        self.api_key = api_key
        self.api_secret = api_secret
        self.log = logging.getLogger(f"{__name__}.{self.name.value}")

    @property
    @abstractmethod
    def name(self) -> ExchangeName:
        raise NotImplementedError

    @abstractmethod
    async def fetch_quote(self, symbol: Symbol) -> Quote:
        raise NotImplementedError

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        close: bool = False,
    ) -> OrderResult:
        """Place a market order. Override in subclass for live trading."""
        raise NotImplementedError(f"{self.name.value} does not support order placement")

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        stop_price: Decimal,
    ) -> str:
        """Place a stop-market order. Returns order_id. Override in subclass."""
        raise NotImplementedError(f"{self.name.value} does not support stop orders")

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        """Cancel an open order. Override in subclass."""
        raise NotImplementedError(f"{self.name.value} does not support order cancellation")

    async def get_position(self, symbol: str) -> PositionInfo:
        raise NotImplementedError(f"{self.name.value} does not support position queries")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        raise NotImplementedError(f"{self.name.value} does not support leverage setting")

    async def get_balance(self) -> BalanceInfo:
        raise NotImplementedError(f"{self.name.value} does not support balance queries")

    async def get_funding_info(self, symbol: str) -> FundingInfo:
        """Return current funding rate and next funding payment time for a perp symbol."""
        raise NotImplementedError(f"{self.name.value} does not support funding queries")

    async def get_min_order_qty(self, symbol: str) -> Decimal:
        raise NotImplementedError(f"{self.name.value} does not support min qty queries")

    async def poll(
        self,
        symbols: Sequence[Symbol],
        on_quote: QuoteCallback,
        stop_event: asyncio.Event,
        poll_interval_sec: float,
        reconnect_backoff_sec: float,
    ) -> None:
        delay = self.inter_request_delay_sec

        async def _delayed_fetch(symbol: Symbol, offset: float) -> Quote:
            if offset > 0:
                await asyncio.sleep(offset)
            return await self.fetch_quote(symbol)

        while not stop_event.is_set():
            try:
                # Launch requests staggered by inter_request_delay_sec.
                # With delay=0 all fire at once (Bybit-style).
                # With delay=0.12 and 20 symbols: first at t=0, last at t=2.3s,
                # requests naturally overlap but never burst.
                results = await asyncio.gather(
                    *(_delayed_fetch(symbol, i * delay) for i, symbol in enumerate(symbols)),
                    return_exceptions=True,
                )

                for symbol, result in zip(symbols, results):
                    if isinstance(result, BaseException):
                        self.log.warning("fetch error for %s: %s", symbol, result)
                        continue
                    callback_result = on_quote(result)
                    if isinstance(callback_result, Awaitable):
                        await callback_result

                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_sec)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning("poll error, retrying after %.2fs: %s", reconnect_backoff_sec, exc)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=reconnect_backoff_sec)
                except TimeoutError:
                    continue
