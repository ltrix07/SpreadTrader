"""Dynamic symbol rotation - discover and rotate high-spread candidates per trading session."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Awaitable, Callable

import aiohttp

from .config import Settings

logger = logging.getLogger(__name__)


def normalize_symbol(raw: str) -> str | None:
    """Normalize symbol to XXXUSDT format. Returns None if not USDT-margined."""
    symbol = raw.upper().replace("_", "").replace("-", "")
    if symbol.endswith("USDT"):
        return symbol
    return None


async def fetch_binance_symbols(session: aiohttp.ClientSession) -> set[str]:
    """GET /fapi/v1/exchangeInfo -> list of USDT perps."""
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    async with session.get(url) as resp:
        data = await resp.json()
    symbols = set()
    for item in data.get("symbols", []):
        if item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT" and item.get("status") == "TRADING":
            sym = normalize_symbol(item["symbol"])
            if sym:
                symbols.add(sym)
    return symbols


async def fetch_bybit_symbols(session: aiohttp.ClientSession) -> set[str]:
    """GET /v5/market/instruments-info?category=linear."""
    url = "https://api.bybit.com/v5/market/instruments-info"
    params = {"category": "linear", "limit": "1000"}
    async with session.get(url, params=params) as resp:
        data = await resp.json()
    symbols = set()
    for item in data.get("result", {}).get("list", []):
        if item.get("quoteCoin") == "USDT" and item.get("status") == "Trading":
            sym = normalize_symbol(item["symbol"])
            if sym:
                symbols.add(sym)
    return symbols


async def fetch_bitget_symbols(session: aiohttp.ClientSession) -> set[str]:
    """GET /api/v2/mix/market/tickers?productType=USDT-FUTURES."""
    url = "https://api.bitget.com/api/v2/mix/market/tickers"
    params = {"productType": "USDT-FUTURES"}
    async with session.get(url, params=params) as resp:
        data = await resp.json()
    symbols = set()
    for item in data.get("data", []):
        sym = normalize_symbol(item.get("symbol", ""))
        if sym:
            symbols.add(sym)
    return symbols


async def fetch_gate_symbols(session: aiohttp.ClientSession) -> set[str]:
    """GET /api/v4/futures/usdt/contracts."""
    url = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
    async with session.get(url) as resp:
        data = await resp.json()
    symbols = set()
    for item in data:
        if item.get("in_delisting"):
            continue
        sym = normalize_symbol(item.get("name", ""))
        if sym:
            symbols.add(sym)
    return symbols


async def fetch_binance_quotes(session: aiohttp.ClientSession, symbols: list[str]) -> dict[str, tuple[float, float]]:
    """Fetch bid/ask for all symbols via /fapi/v1/ticker/bookTicker."""
    url = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
    async with session.get(url) as resp:
        data = await resp.json()
    result: dict[str, tuple[float, float]] = {}
    for item in data:
        sym = item.get("symbol", "")
        if sym in symbols:
            bid = float(item.get("bidPrice", 0))
            ask = float(item.get("askPrice", 0))
            if bid > 0 and ask > 0:
                result[sym] = (bid, ask)
    return result


async def fetch_bybit_quotes(session: aiohttp.ClientSession, symbols: list[str]) -> dict[str, tuple[float, float]]:
    """Fetch bid/ask via /v5/market/tickers?category=linear."""
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "linear"}
    async with session.get(url, params=params) as resp:
        data = await resp.json()
    result: dict[str, tuple[float, float]] = {}
    for item in data.get("result", {}).get("list", []):
        sym = item.get("symbol", "")
        if sym in symbols:
            bid = float(item.get("bid1Price", 0))
            ask = float(item.get("ask1Price", 0))
            if bid > 0 and ask > 0:
                result[sym] = (bid, ask)
    return result


async def fetch_bitget_quotes(session: aiohttp.ClientSession, symbols: list[str]) -> dict[str, tuple[float, float]]:
    """Fetch bid/ask via /api/v2/mix/market/tickers."""
    url = "https://api.bitget.com/api/v2/mix/market/tickers"
    params = {"productType": "USDT-FUTURES"}
    async with session.get(url, params=params) as resp:
        data = await resp.json()
    result: dict[str, tuple[float, float]] = {}
    for item in data.get("data", []):
        sym = normalize_symbol(item.get("symbol", ""))
        if sym and sym in symbols:
            bid = float(item.get("bidPr", 0))
            ask = float(item.get("askPr", 0))
            if bid > 0 and ask > 0:
                result[sym] = (bid, ask)
    return result


async def fetch_gate_quotes(session: aiohttp.ClientSession, symbols: list[str]) -> dict[str, tuple[float, float]]:
    """Fetch bid/ask via /api/v4/futures/usdt/tickers."""
    url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
    async with session.get(url) as resp:
        data = await resp.json()
    result: dict[str, tuple[float, float]] = {}
    for item in data:
        sym = normalize_symbol(item.get("contract", ""))
        if sym and sym in symbols:
            bid_str = item.get("highest_bid", "0")
            ask_str = item.get("lowest_ask", "0")
            bid = float(bid_str) if bid_str else 0
            ask = float(ask_str) if ask_str else 0
            if bid > 0 and ask > 0:
                result[sym] = (bid, ask)
    return result


@dataclass(slots=True)
class SymbolSpread:
    symbol: str
    max_spread_pct: float
    best_pair: str
    spreads: dict[str, float]
    num_exchanges: int
    max_bbo_bps: float


def calc_spreads(symbol: str, quotes: dict[str, tuple[float, float]]) -> SymbolSpread | None:
    """Calculate max spread across all exchange pairs for a symbol."""
    exchanges = list(quotes.keys())
    if len(exchanges) < 2:
        return None

    spreads: dict[str, float] = {}
    max_spread = 0.0
    best_pair = ""
    max_bbo = 0.0

    for _exchange_name, (bid, ask) in quotes.items():
        bbo_bps = (ask - bid) / bid * 10_000 if bid > 0 else 0.0
        max_bbo = max(max_bbo, bbo_bps)

    for ex_a, ex_b in combinations(exchanges, 2):
        bid_a, ask_a = quotes[ex_a]
        bid_b, ask_b = quotes[ex_b]

        spread_ab = (bid_a - ask_b) / ask_b * 100 if ask_b > 0 else 0
        spread_ba = (bid_b - ask_a) / ask_a * 100 if ask_a > 0 else 0

        best = max(spread_ab, spread_ba)
        pair_name = f"{ex_a}<->{ex_b}"
        spreads[pair_name] = best

        if best > max_spread:
            max_spread = best
            if spread_ab >= spread_ba:
                best_pair = f"{ex_b}->{ex_a}"
            else:
                best_pair = f"{ex_a}->{ex_b}"

    return SymbolSpread(
        symbol=symbol,
        max_spread_pct=max_spread,
        best_pair=best_pair,
        spreads=spreads,
        num_exchanges=len(exchanges),
        max_bbo_bps=max_bbo,
    )


async def discover_candidates(
    session: aiohttp.ClientSession,
    base_symbols: set[str],
    min_spread_pct: float = 0.08,
    max_bbo_bps: float = 15.0,
    min_exchanges: int = 3,
    max_symbols: int = 15,
) -> list[str]:
    """Discover top spread candidates across exchanges.

    Returns symbol names (e.g. ["TACUSDT", "ALCHUSDT"]) excluding base_symbols.
    """
    if max_symbols <= 0:
        return []

    exchange_names = ["binance", "bybit", "bitget", "gate"]
    listing_results = await asyncio.gather(
        fetch_binance_symbols(session),
        fetch_bybit_symbols(session),
        fetch_bitget_symbols(session),
        fetch_gate_symbols(session),
        return_exceptions=True,
    )

    exchange_symbols: dict[str, set[str]] = {}
    for name, result in zip(exchange_names, listing_results):
        if isinstance(result, Exception):
            logger.warning("failed to fetch %s symbols: %s", name, result)
            exchange_symbols[name] = set()
            continue
        exchange_symbols[name] = result

    all_symbols: set[str] = set()
    for symbols in exchange_symbols.values():
        all_symbols |= symbols

    candidates: list[str] = []
    for symbol in sorted(all_symbols):
        on_exchanges = [ex for ex in exchange_names if symbol in exchange_symbols[ex]]
        if len(on_exchanges) >= min_exchanges:
            candidates.append(symbol)

    candidates = [symbol for symbol in candidates if symbol not in base_symbols]
    if not candidates:
        return []

    quote_results = await asyncio.gather(
        fetch_binance_quotes(session, candidates),
        fetch_bybit_quotes(session, candidates),
        fetch_bitget_quotes(session, candidates),
        fetch_gate_quotes(session, candidates),
        return_exceptions=True,
    )

    all_quotes: dict[str, dict[str, tuple[float, float]]] = {}
    for name, result in zip(exchange_names, quote_results):
        if isinstance(result, Exception):
            logger.warning("failed to fetch %s quotes: %s", name, result)
            continue
        for symbol, quote in result.items():
            all_quotes.setdefault(symbol, {})[name] = quote

    spread_results: list[SymbolSpread] = []
    for symbol in sorted(all_quotes):
        spread = calc_spreads(symbol, all_quotes[symbol])
        if spread is None:
            continue
        if max_bbo_bps > 0 and spread.max_bbo_bps > max_bbo_bps:
            continue
        if spread.max_spread_pct >= min_spread_pct:
            spread_results.append(spread)

    spread_results.sort(key=lambda item: item.max_spread_pct, reverse=True)
    return [item.symbol for item in spread_results[:max_symbols]]


class SymbolRotator:
    """Manages dynamic symbol rotation per trading session."""

    def __init__(
        self,
        settings: Settings,
        get_open_position_count: Callable[[], int],
        get_pending_entry_count: Callable[[], int],
        on_symbols_changed: Callable[[list[str], list[str]], Awaitable[None]],
    ) -> None:
        self.settings = settings
        self.log = logging.getLogger(__name__)
        self._get_open_position_count = get_open_position_count
        self._get_pending_entry_count = get_pending_entry_count
        self._on_symbols_changed = on_symbols_changed
        self.scanning = False
        self.current_dynamic_symbols: set[str] = set()
        self.last_rotation_at: datetime | None = None
        self._session_times: list[tuple[int, int]] = []
        self._parse_session_times()

    def _parse_session_times(self) -> None:
        for time_str in self.settings.dynamic_session_times_utc:
            try:
                parts = time_str.strip().split(":")
                if len(parts) != 2:
                    raise ValueError("invalid time format")
                hour = int(parts[0])
                minute = int(parts[1])
                if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                    raise ValueError("time out of range")
                self._session_times.append((hour, minute))
            except Exception as exc:  # noqa: BLE001
                self.log.warning("ignoring invalid dynamic session time %r: %s", time_str, exc)

        if not self._session_times:
            self._session_times = [(0, 30), (8, 30), (16, 30)]
            self.log.warning("no valid session times configured, using defaults %s", self._session_times)

    async def run(self, stop_event: asyncio.Event, http_session: aiohttp.ClientSession) -> None:
        """Main loop: wait for session boundaries, then rotate if conditions met."""
        self.log.info(
            "symbol rotator started | max_dynamic=%d | sessions=%s",
            self.settings.dynamic_max_symbols,
            self.settings.dynamic_session_times_utc,
        )

        while not stop_event.is_set():
            now = datetime.now(UTC)
            next_rotation = self._next_session_time(now)
            wait_seconds = (next_rotation - now).total_seconds()

            if wait_seconds > 0:
                self.log.info(
                    "next rotation at %s (in %.0f min)",
                    next_rotation.strftime("%H:%M UTC"),
                    wait_seconds / 60,
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
                    return
                except TimeoutError:
                    pass

            await self._wait_for_no_positions(stop_event)
            if stop_event.is_set():
                return

            await self._do_rotation(http_session)

    async def _wait_for_no_positions(self, stop_event: asyncio.Event) -> None:
        """Wait until there are no open positions or pending entries."""
        retry_sec = self.settings.dynamic_retry_interval_sec
        while not stop_event.is_set():
            open_count = self._get_open_position_count()
            pending_count = self._get_pending_entry_count()
            if open_count == 0 and pending_count == 0:
                return
            self.log.info(
                "rotation waiting | open=%d pending=%d | retry in %.0fs",
                open_count,
                pending_count,
                retry_sec,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=retry_sec)
                return
            except TimeoutError:
                pass

    async def _do_rotation(self, http_session: aiohttp.ClientSession) -> None:
        """Execute the rotation: scan, diff, notify."""
        self.scanning = True
        self.log.info("rotation scan starting...")
        try:
            base_symbols = set(self.settings.symbols)
            new_dynamic = await asyncio.wait_for(
                discover_candidates(
                    session=http_session,
                    base_symbols=base_symbols,
                    min_spread_pct=self.settings.dynamic_min_spread_pct,
                    max_bbo_bps=self.settings.dynamic_max_bbo_bps,
                    min_exchanges=self.settings.dynamic_require_exchanges,
                    max_symbols=self.settings.dynamic_max_symbols,
                ),
                timeout=self.settings.dynamic_scan_timeout_sec,
            )

            new_set = set(new_dynamic)
            old_set = self.current_dynamic_symbols
            added = sorted(new_set - old_set)
            removed = sorted(old_set - new_set)

            self.current_dynamic_symbols = new_set
            self.last_rotation_at = datetime.now(UTC)

            self.log.info(
                "rotation complete | dynamic=%d | added=%s | removed=%s",
                len(new_set),
                added if added else "none",
                removed if removed else "none",
            )

            if added or removed:
                await self._on_symbols_changed(added, removed)
        except TimeoutError:
            self.log.error(
                "rotation scan timed out after %.1fs",
                self.settings.dynamic_scan_timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.error("rotation scan failed: %s", exc, exc_info=True)
        finally:
            self.scanning = False

    def _next_session_time(self, now: datetime) -> datetime:
        """Find the next session rotation time."""
        candidates: list[datetime] = []
        for hour, minute in self._session_times:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            candidates.append(candidate)
        return min(candidates)

    def get_all_active_symbols(self) -> list[str]:
        """Return base + dynamic symbols (deduplicated)."""
        base = set(self.settings.symbols)
        return sorted(base | self.current_dynamic_symbols)

