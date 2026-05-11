from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence

from aiohttp import ClientSession

from ..models import ExchangeName, Quote, Symbol

QuoteCallback = Callable[[Quote], Awaitable[None] | None]


class ExchangeClient(ABC):
    def __init__(self, session: ClientSession, request_timeout_sec: float = 8.0) -> None:
        self.session = session
        self.request_timeout_sec = request_timeout_sec
        self.log = logging.getLogger(f"{__name__}.{self.name.value}")

    @property
    @abstractmethod
    def name(self) -> ExchangeName:
        raise NotImplementedError

    @abstractmethod
    async def fetch_quote(self, symbol: Symbol) -> Quote:
        raise NotImplementedError

    async def poll(
        self,
        symbols: Sequence[Symbol],
        on_quote: QuoteCallback,
        stop_event: asyncio.Event,
        poll_interval_sec: float,
        reconnect_backoff_sec: float,
    ) -> None:
        while not stop_event.is_set():
            try:
                for symbol in symbols:
                    quote = await self.fetch_quote(symbol)
                    callback_result = on_quote(quote)
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

