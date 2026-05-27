from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import aiohttp

from spread_arb.config import Settings
from spread_arb.models import ExchangeName, Quote
from spread_arb.scanner import QuoteScanner
from spread_arb.symbol_rotator import SymbolRotator


class FakeRotator:
    def __init__(self, active_symbols: list[str], dynamic_symbols: list[str]) -> None:
        self._active_symbols = list(active_symbols)
        self.current_dynamic_symbols = set(dynamic_symbols)
        self.scanning = False

    def get_all_active_symbols(self) -> list[str]:
        return list(self._active_symbols)


class PollCaptureExchange:
    def __init__(self) -> None:
        self.received_symbols: Sequence[str] | None = None
        self.snapshots: list[list[str]] = []
        self.name = ExchangeName.MEXC

    async def poll(
        self,
        symbols: Sequence[str],
        on_quote,
        stop_event: asyncio.Event,
        poll_interval_sec: float,
        reconnect_backoff_sec: float,
    ) -> None:
        _ = on_quote, poll_interval_sec, reconnect_backoff_sec
        self.received_symbols = symbols
        self.snapshots.append(list(symbols))
        await asyncio.sleep(0)
        self.snapshots.append(list(symbols))
        await stop_event.wait()


def _scanner(symbols: list[str]) -> QuoteScanner:
    return QuoteScanner(
        Settings(
            symbols=symbols,
            exchanges=[ExchangeName.MEXC],
            dynamic_rotation_enabled=True,
        )
    )


def test_start_rest_polls_uses_active_dynamic_symbol_list() -> None:
    async def _run() -> None:
        scanner = _scanner(["BTCUSDT"])
        scanner.symbol_rotator = cast(SymbolRotator, FakeRotator(["BTCUSDT", "ETHUSDT"], ["ETHUSDT"]))
        scanner._reset_rest_poll_symbols()
        exchange = PollCaptureExchange()

        tasks = scanner._start_rest_polls([cast(object, exchange)])
        await asyncio.sleep(0.05)

        assert exchange.received_symbols is scanner._rest_poll_symbols
        assert list(scanner._rest_poll_symbols) == ["BTCUSDT", "ETHUSDT"]
        assert exchange.snapshots[0] == ["BTCUSDT", "ETHUSDT"]

        scanner.stop_event.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_run())


def test_dynamic_symbol_changes_update_rest_poll_symbols_in_place() -> None:
    async def _run() -> None:
        scanner = _scanner(["BTCUSDT"])
        scanner.symbol_rotator = cast(SymbolRotator, FakeRotator(["BTCUSDT", "SOLUSDT"], ["SOLUSDT"]))
        scanner._rest_poll_symbols[:] = ["BTCUSDT", "ETHUSDT"]
        exchange = PollCaptureExchange()

        tasks = scanner._start_rest_polls([cast(object, exchange)])
        await asyncio.sleep(0)

        await scanner._on_dynamic_symbols_changed(["SOLUSDT"], ["ETHUSDT"])
        await asyncio.sleep(0.05)

        assert exchange.received_symbols is scanner._rest_poll_symbols
        assert list(scanner._rest_poll_symbols) == ["BTCUSDT", "SOLUSDT"]
        assert ["BTCUSDT", "SOLUSDT"] in exchange.snapshots

        scanner.stop_event.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_run())
