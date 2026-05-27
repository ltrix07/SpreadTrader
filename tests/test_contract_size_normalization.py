from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import cast

import aiohttp

from spread_arb.exchanges.gate import GateExchange
from spread_arb.exchanges.okx import OkxExchange


class _DummyResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def __aenter__(self) -> _DummyResponse:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict[str, object]:
        return self.payload


class _DummySession:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = list(payloads)

    def get(self, *_args, **_kwargs) -> _DummyResponse:
        return _DummyResponse(self.payloads.pop(0))


def test_gate_fetch_quote_normalizes_contract_sizes_to_base_units() -> None:
    async def _run() -> None:
        session = _DummySession([
            {
                "bids": [{"p": "100.0", "s": "500"}],
                "asks": [{"p": "100.1", "s": "750"}],
            },
        ])
        exchange = GateExchange(session=cast(aiohttp.ClientSession, session))
        exchange._contract_cache["SOL_USDT"] = {"quanto_multiplier": "0.001"}

        quote = await exchange.fetch_quote("SOLUSDT")

        assert quote.best_bid_size == Decimal("0.500")
        assert quote.best_ask_size == Decimal("0.750")

    asyncio.run(_run())


def test_okx_fetch_quote_normalizes_contract_sizes_to_base_units() -> None:
    async def _run() -> None:
        session = _DummySession([
            {
                "code": "0",
                "data": [{
                    "bids": [["100.0", "5", "0", "1"]],
                    "asks": [["100.1", "7", "0", "1"]],
                    "ts": "1712345678901",
                }],
            },
        ])
        exchange = OkxExchange(session=cast(aiohttp.ClientSession, session))
        exchange._instrument_cache["SOL-USDT-SWAP"] = {
            "ctVal": Decimal("0.1"),
            "minSz": Decimal("1"),
            "lotSz": Decimal("1"),
        }

        quote = await exchange.fetch_quote("SOLUSDT")

        assert quote.best_bid_size == Decimal("0.5")
        assert quote.best_ask_size == Decimal("0.7")

    asyncio.run(_run())
