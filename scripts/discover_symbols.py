#!/usr/bin/env python3
"""
Symbol discovery: find new high-spread candidates across exchanges.

Fetches all USDT perpetual futures from binance/bybit/bitget/gate,
finds common symbols, grabs live quotes, and ranks by cross-exchange spread.

Usage:
    python scripts/discover_symbols.py [--top 30] [--min-spread 0.10]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from itertools import combinations

import aiohttp

# Current symbols in config (to mark as "already tracked")
CURRENT_SYMBOLS = {
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "SUIUSDT", "SEIUSDT", "WIFUSDT", "PEPEUSDT",
    "FLOKIUSDT", "INJUSDT", "NEARUSDT", "ORDIUSDT",
    "1000BONKUSDT",
    "DOTUSDT", "ATOMUSDT", "FILUSDT", "UNIUSDT", "LDOUSDT",
    "AAVEUSDT", "GRTUSDT", "RUNEUSDT", "TIAUSDT", "STXUSDT",
    "FETUSDT", "PENDLEUSDT", "JUPUSDT", "ENAUSDT", "ONDOUSDT",
    "ZKUSDT", "STRKUSDT", "BLURUSDT", "DYDXUSDT", "GALAUSDT",
    "CFXUSDT", "IMXUSDT", "GMXUSDT", "MASKUSDT", "WOOUSDT",
    "ACHUSDT", "CELOUSDT", "LRCUSDT", "SKLUSDT", "ZENUSDT",
}


def normalize_symbol(raw: str) -> str | None:
    """Normalize symbol to XXXUSDT format. Returns None if not USDT-margined."""
    s = raw.upper().replace("_", "").replace("-", "")
    # Gate uses BTC_USDT format
    if s.endswith("USDT"):
        return s
    return None


# ---------------------------------------------------------------------------
# Exchange listing fetchers
# ---------------------------------------------------------------------------

async def fetch_binance_symbols(session: aiohttp.ClientSession) -> set[str]:
    """GET /fapi/v1/exchangeInfo -> list of USDT perps."""
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    async with session.get(url) as resp:
        data = await resp.json()
    symbols = set()
    for s in data.get("symbols", []):
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            sym = normalize_symbol(s["symbol"])
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
        raw = item.get("symbol", "")
        sym = normalize_symbol(raw)
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
        raw = item.get("name", "")  # BTC_USDT
        sym = normalize_symbol(raw)
        if sym:
            symbols.add(sym)
    return symbols


# ---------------------------------------------------------------------------
# Quote fetchers (batch)
# ---------------------------------------------------------------------------

async def fetch_binance_quotes(session: aiohttp.ClientSession, symbols: list[str]) -> dict[str, tuple[float, float]]:
    """Fetch bid/ask for all symbols via /fapi/v1/ticker/bookTicker."""
    url = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
    async with session.get(url) as resp:
        data = await resp.json()
    result = {}
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
    result = {}
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
    result = {}
    for item in data.get("data", []):
        raw = item.get("symbol", "")
        sym = normalize_symbol(raw)
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
    result = {}
    for item in data:
        raw = item.get("contract", "")
        sym = normalize_symbol(raw)
        if sym and sym in symbols:
            bid_str = item.get("highest_bid", "0")
            ask_str = item.get("lowest_ask", "0")
            bid = float(bid_str) if bid_str else 0
            ask = float(ask_str) if ask_str else 0
            if bid > 0 and ask > 0:
                result[sym] = (bid, ask)
    return result


# ---------------------------------------------------------------------------
# Spread calculation
# ---------------------------------------------------------------------------

@dataclass
class SymbolSpread:
    symbol: str
    max_spread_pct: float
    best_pair: str
    spreads: dict[str, float]
    num_exchanges: int
    is_new: bool
    max_bbo_bps: float


def calc_spreads(
    symbol: str,
    quotes: dict[str, tuple[float, float]],
) -> SymbolSpread | None:
    """Calculate max spread across all exchange pairs for a symbol."""
    exchanges = list(quotes.keys())
    if len(exchanges) < 2:
        return None

    spreads = {}
    max_spread = 0.0
    best_pair = ""
    max_bbo = 0.0

    for _ex_name, (bid, ask) in quotes.items():
        bbo_bps = (ask - bid) / bid * 10_000 if bid > 0 else 0.0
        max_bbo = max(max_bbo, bbo_bps)

    for ex_a, ex_b in combinations(exchanges, 2):
        bid_a, ask_a = quotes[ex_a]
        bid_b, ask_b = quotes[ex_b]

        # Spread A->B: buy on B (ask_b), sell on A (bid_a)
        spread_ab = (bid_a - ask_b) / ask_b * 100 if ask_b > 0 else 0
        # Spread B->A: buy on A (ask_a), sell on B (bid_b)
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
        is_new=symbol not in CURRENT_SYMBOLS,
        max_bbo_bps=max_bbo,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(top_n: int, min_spread: float, show_current: bool, max_bbo: float) -> None:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Step 1: Fetch all symbols from each exchange
        print("Fetching symbol lists from 4 exchanges...")
        results = await asyncio.gather(
            fetch_binance_symbols(session),
            fetch_bybit_symbols(session),
            fetch_bitget_symbols(session),
            fetch_gate_symbols(session),
            return_exceptions=True,
        )

        exchange_names = ["binance", "bybit", "bitget", "gate"]
        exchange_symbols: dict[str, set[str]] = {}
        for name, res in zip(exchange_names, results):
            if isinstance(res, Exception):
                print(f"  ERROR fetching {name}: {res}")
                exchange_symbols[name] = set()
            else:
                exchange_symbols[name] = res
                print(f"  {name}: {len(res)} USDT perps")

        # Step 2: Find symbols on at least 3 exchanges
        all_symbols = set()
        for syms in exchange_symbols.values():
            all_symbols |= syms

        common_3plus: dict[str, list[str]] = {}
        for sym in sorted(all_symbols):
            on_exchanges = [ex for ex in exchange_names if sym in exchange_symbols[ex]]
            if len(on_exchanges) >= 3:
                common_3plus[sym] = on_exchanges

        print(f"\nSymbols on ≥3 exchanges: {len(common_3plus)}")
        common_all4 = {s for s, exs in common_3plus.items() if len(exs) == 4}
        print(f"Symbols on all 4 exchanges: {len(common_all4)}")

        candidates = list(common_3plus.keys())
        if not show_current:
            new_only = [s for s in candidates if s not in CURRENT_SYMBOLS]
            print(f"New candidates (not in current list): {len(new_only)}")
        else:
            new_only = candidates

        # Step 3: Fetch live quotes
        print("\nFetching live quotes from all exchanges...")
        quote_results = await asyncio.gather(
            fetch_binance_quotes(session, new_only),
            fetch_bybit_quotes(session, new_only),
            fetch_bitget_quotes(session, new_only),
            fetch_gate_quotes(session, new_only),
            return_exceptions=True,
        )

        # Merge quotes per symbol
        all_quotes: dict[str, dict[str, tuple[float, float]]] = {}
        for name, res in zip(exchange_names, quote_results):
            if isinstance(res, Exception):
                print(f"  ERROR fetching quotes from {name}: {res}")
                continue
            print(f"  {name}: {len(res)} quotes")
            for sym, bbo in res.items():
                if sym not in all_quotes:
                    all_quotes[sym] = {}
                all_quotes[sym][name] = bbo

        # Step 4: Calculate spreads
        print(f"\nCalculating spreads for {len(all_quotes)} symbols...")
        results_list: list[SymbolSpread] = []
        bbo_filtered_count = 0
        for sym in sorted(all_quotes):
            spread = calc_spreads(sym, all_quotes[sym])
            if spread is None:
                continue
            if max_bbo > 0 and spread.max_bbo_bps > max_bbo:
                bbo_filtered_count += 1
                continue
            if spread.max_spread_pct >= min_spread:
                results_list.append(spread)

        results_list.sort(key=lambda x: x.max_spread_pct, reverse=True)

        # Step 5: Print results
        print(f"\n{'='*96}")
        print(f"TOP {top_n} SYMBOLS BY CURRENT SPREAD (min {min_spread}%)")
        print(f"{'='*96}")
        print(f"{'#':>3} {'Symbol':<18} {'Spread%':>8} {'Direction':<20} {'Exch':>4} {'BBO_bps':>8} {'Status':<8}")
        print("-" * 96)

        for i, s in enumerate(results_list[:top_n], 1):
            status = "NEW" if s.is_new else "current"
            print(
                f"{i:>3} {s.symbol:<18} {s.max_spread_pct:>8.4f} {s.best_pair:<20} "
                f"{s.num_exchanges:>4} {s.max_bbo_bps:>8.1f} {status:<8}"
            )

        print(f"Filtered by BBO (>{max_bbo} bps): {bbo_filtered_count}")

        # Summary for new symbols
        new_candidates = [s for s in results_list if s.is_new]
        print(f"\n{'='*80}")
        print(f"NEW CANDIDATES ABOVE {min_spread}%: {len(new_candidates)}")
        print(f"{'='*80}")
        if new_candidates:
            syms = [s.symbol for s in new_candidates[:top_n]]
            print(f"Suggested additions: {','.join(syms)}")

        # Also show current symbols' spreads for comparison
        if show_current:
            current_in_results = [s for s in results_list if not s.is_new]
            print(f"\nCurrent symbols in results: {len(current_in_results)}")

        # Output a ready-to-use SYMBOLS line
        print(f"\n{'='*80}")
        print("SUGGESTED .env SYMBOLS LINE (top performers):")
        print(f"{'='*80}")
        # Keep current symbols that are performing + add new ones
        keep_current = {s.symbol for s in results_list if not s.is_new and s.max_spread_pct >= 0.05}
        add_new = {s.symbol for s in new_candidates[:15] if s.max_spread_pct >= min_spread}
        final = sorted(keep_current | add_new)
        print(f"SYMBOLS={','.join(final)}")
        print(f"Total: {len(final)} symbols")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover high-spread symbols across exchanges")
    parser.add_argument("--top", type=int, default=50, help="Show top N results (default: 50)")
    parser.add_argument("--min-spread", type=float, default=0.05, help="Min spread %% to show (default: 0.05)")
    parser.add_argument("--max-bbo", type=float, default=15.0, help="Max BBO spread in bps per exchange (default: 15)")
    parser.add_argument("--all", action="store_true", help="Show current + new symbols (default: new only)")
    args = parser.parse_args()

    asyncio.run(run(args.top, args.min_spread, args.all, args.max_bbo))


if __name__ == "__main__":
    main()
