from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from spread_arb.config import Settings
from spread_arb.models import ExchangeName, Quote
from spread_arb.scanner import QuoteScanner


class FakeStopEvent:
    def __init__(self) -> None:
        self._is_set = False
        self._wait_calls = 0

    def is_set(self) -> bool:
        return self._is_set

    async def wait(self) -> bool:
        self._wait_calls += 1
        if self._wait_calls == 1:
            raise TimeoutError
        self._is_set = True
        return True


class FakeOpportunityStore:
    def __init__(self, *, should_fail: bool) -> None:
        self.should_fail = should_fail
        self.insert_calls = 0

    def insert_spread_snapshots(self, records) -> int:
        self.insert_calls += 1
        if self.should_fail:
            raise RuntimeError("db write failed")
        return len(records)


class FakeMeanReversionEngine:
    def __init__(self) -> None:
        self.update_calls = 0

    def update_baselines(self, snapshot_quotes) -> None:
        self.update_calls += 1


def _quote(*, exchange: ExchangeName, symbol: str, bid: str, ask: str) -> Quote:
    return Quote(
        exchange=exchange,
        symbol=symbol,
        best_bid_price=Decimal(bid),
        best_ask_price=Decimal(ask),
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("100"),
        receive_latency_ms=0.0,
        source_latency_ms=0.0,
        received_at=datetime.now(UTC),
    )


def test_snapshot_loop_updates_baselines_only_after_successful_persist() -> None:
    async def _run() -> None:
        scanner = QuoteScanner(
            Settings(
                symbols=["BTCUSDT"],
                exchanges=[ExchangeName.BYBIT, ExchangeName.OKX],
            )
        )
        scanner.stop_event = FakeStopEvent()  # type: ignore[assignment]
        scanner.opportunity_store = FakeOpportunityStore(should_fail=False)  # type: ignore[assignment]
        scanner.mean_reversion_engine = FakeMeanReversionEngine()  # type: ignore[assignment]
        scanner.latest_quotes = {
            (ExchangeName.BYBIT, "BTCUSDT"): _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="100", ask="100.1"),
            (ExchangeName.OKX, "BTCUSDT"): _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="100.5", ask="100.6"),
        }

        await scanner._collect_spread_snapshots()

        assert scanner.opportunity_store.insert_calls == 1
        assert scanner.mean_reversion_engine.update_calls == 1

    asyncio.run(_run())


def test_snapshot_loop_skips_baseline_update_when_persist_fails() -> None:
    async def _run() -> None:
        scanner = QuoteScanner(
            Settings(
                symbols=["BTCUSDT"],
                exchanges=[ExchangeName.BYBIT, ExchangeName.OKX],
            )
        )
        scanner.stop_event = FakeStopEvent()  # type: ignore[assignment]
        scanner.opportunity_store = FakeOpportunityStore(should_fail=True)  # type: ignore[assignment]
        scanner.mean_reversion_engine = FakeMeanReversionEngine()  # type: ignore[assignment]
        scanner.latest_quotes = {
            (ExchangeName.BYBIT, "BTCUSDT"): _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="100", ask="100.1"),
            (ExchangeName.OKX, "BTCUSDT"): _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="100.5", ask="100.6"),
        }

        await scanner._collect_spread_snapshots()

        assert scanner.opportunity_store.insert_calls == 1
        assert scanner.mean_reversion_engine.update_calls == 0

    asyncio.run(_run())
