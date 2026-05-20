#!/usr/bin/env python3
"""
Test script: opens and immediately closes a small position to verify execution works.

Usage:
    python scripts/test_execution.py --exchange binance --symbol SOLUSDT --notional 10

Requires API keys in .env.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal
from pathlib import Path

import aiohttp

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from spread_arb.config import get_settings
from spread_arb.models import ExchangeName


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("test_execution")


EXCHANGE_MAP = {
    "binance": ExchangeName.BINANCE,
    "okx": ExchangeName.OKX,
    "bybit": ExchangeName.BYBIT,
    "bitget": ExchangeName.BITGET,
    "mexc": ExchangeName.MEXC,
}


def get_client_class(exchange: ExchangeName):
    """Import and return the correct exchange client class."""
    if exchange == ExchangeName.BINANCE:
        from spread_arb.exchanges.binance import BinanceExchange
        return BinanceExchange
    if exchange == ExchangeName.OKX:
        from spread_arb.exchanges.okx import OkxExchange
        return OkxExchange
    if exchange == ExchangeName.BYBIT:
        from spread_arb.exchanges.bybit import BybitExchange
        return BybitExchange
    if exchange == ExchangeName.BITGET:
        from spread_arb.exchanges.bitget import BitgetExchange
        return BitgetExchange
    if exchange == ExchangeName.MEXC:
        from spread_arb.exchanges.mexc import MexcExchange
        return MexcExchange
    raise ValueError(f"Unsupported exchange: {exchange}")


def get_credentials(settings, exchange: ExchangeName) -> dict:
    """Extract API credentials for the given exchange."""
    if exchange == ExchangeName.BINANCE:
        return {"api_key": settings.api_key_binance, "api_secret": settings.api_secret_binance}
    if exchange == ExchangeName.OKX:
        return {
            "api_key": settings.api_key_okx,
            "api_secret": settings.api_secret_okx,
            "passphrase": settings.api_passphrase_okx,
        }
    if exchange == ExchangeName.BYBIT:
        return {"api_key": settings.api_key_bybit, "api_secret": settings.api_secret_bybit}
    if exchange == ExchangeName.BITGET:
        return {
            "api_key": settings.api_key_bitget,
            "api_secret": settings.api_secret_bitget,
            "passphrase": settings.api_passphrase_bitget,
        }
    if exchange == ExchangeName.MEXC:
        return {"api_key": settings.api_key_mexc, "api_secret": settings.api_secret_mexc}
    raise ValueError(f"No credentials for {exchange}")


async def run_test(exchange_name: str, symbol: str, notional: float, leverage: int) -> None:
    settings = get_settings()
    exchange = EXCHANGE_MAP[exchange_name]
    creds = get_credentials(settings, exchange)

    if not creds.get("api_key"):
        log.error("No API key configured for %s. Set API_KEY_%s in .env", exchange_name, exchange_name.upper())
        return

    ClientClass = get_client_class(exchange)

    async with aiohttp.ClientSession() as session:
        client = ClientClass(
            session=session,
            request_timeout_sec=settings.request_timeout_sec,
            **creds,
        )

        log.info("=" * 50)
        log.info("Testing %s on %s", symbol, exchange_name.upper())
        log.info("=" * 50)

        balance = await client.get_balance()
        log.info("Balance: total=%.2f USDT, available=%.2f USDT", balance.total_usdt, balance.available_usdt)

        if float(balance.available_usdt) < notional:
            log.error("Insufficient balance: need %.2f, have %.2f", notional, balance.available_usdt)
            return

        log.info("Setting leverage to %dx...", leverage)
        await client.set_leverage(symbol, leverage)
        log.info("Leverage set.")

        quote = await client.fetch_quote(symbol)
        ask_price = float(quote.best_ask_price)
        bid_price = float(quote.best_bid_price)
        log.info(
            "Current price: ask=%.4f, bid=%.4f, spread=%.4f%%",
            ask_price,
            bid_price,
            (ask_price - bid_price) / bid_price * 100,
        )

        from decimal import ROUND_DOWN
        qty = Decimal(str(notional)) / Decimal(str(ask_price))

        try:
            min_qty = await client.get_min_order_qty(symbol)
            # Round qty down to step size
            if min_qty > 0:
                qty = (qty / min_qty).to_integral_value(rounding=ROUND_DOWN) * min_qty
            log.info("Min order qty: %s, our qty: %s", min_qty, qty)
            if qty < min_qty:
                log.error(
                    "Qty too small! Need at least %s, have %s. Increase --notional or use a cheaper symbol.",
                    min_qty,
                    qty,
                )
                return
        except NotImplementedError:
            log.warning("Min qty check not available, proceeding...")

        log.info("Opening LONG position: qty=%s (~$%.2f notional)...", qty, notional)
        entry = await client.place_market_order(symbol, "buy", qty)
        log.info(
            "ENTRY FILLED: order_id=%s, qty=%s, avg_price=%s, fee=%s %s",
            entry.order_id,
            entry.filled_qty,
            entry.avg_price,
            entry.fee,
            entry.fee_currency,
        )

        stop_order_id = ""
        stop_pct = settings.exchange_stop_loss_pct / 100.0
        stop_price = entry.avg_price * Decimal(str(1.0 - stop_pct))
        try:
            log.info(
                "Placing protective stop: side=sell qty=%s stop_price=%s (-%.2f%%)...",
                entry.filled_qty,
                stop_price,
                settings.exchange_stop_loss_pct,
            )
            stop_order_id = await client.place_stop_market_order(
                symbol=symbol,
                side="sell",
                qty=entry.filled_qty,
                stop_price=stop_price,
            )
            log.info("STOP PLACED: order_id=%s", stop_order_id or "<empty>")
        except Exception as exc:
            log.warning("Failed to place stop order: %s", exc)

        if stop_order_id:
            try:
                log.info("Cancelling protective stop: order_id=%s...", stop_order_id)
                await client.cancel_order(symbol, stop_order_id)
                log.info("STOP CANCELLED: order_id=%s", stop_order_id)
            except Exception as exc:
                log.warning("Failed to cancel stop order: %s", exc)

        log.info("Closing position: selling qty=%s...", entry.filled_qty)
        exit_order = await client.place_market_order(symbol, "sell", entry.filled_qty, close=True)
        log.info(
            "EXIT FILLED: order_id=%s, qty=%s, avg_price=%s, fee=%s %s",
            exit_order.order_id,
            exit_order.filled_qty,
            exit_order.avg_price,
            exit_order.fee,
            exit_order.fee_currency,
        )

        entry_cost = float(entry.avg_price) * float(entry.filled_qty)
        exit_revenue = float(exit_order.avg_price) * float(exit_order.filled_qty)
        total_fees = float(entry.fee) + float(exit_order.fee)
        pnl = exit_revenue - entry_cost - total_fees

        log.info("=" * 50)
        log.info("ROUND-TRIP SUMMARY")
        log.info("  Entry: %s @ %s = $%.4f", entry.filled_qty, entry.avg_price, entry_cost)
        log.info("  Exit:  %s @ %s = $%.4f", exit_order.filled_qty, exit_order.avg_price, exit_revenue)
        log.info("  Fees:  $%.4f", total_fees)
        log.info("  Net PnL: $%.4f", pnl)
        log.info(
            "  Slippage from mid: %.4f%%",
            (float(entry.avg_price) - float(exit_order.avg_price)) / float(entry.avg_price) * 100,
        )
        log.info("=" * 50)

        try:
            pos = await client.get_position(symbol)
            log.info("Current position: size=%s (should be ~0)", pos.size)
        except Exception as exc:
            log.warning("Could not verify position: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test exchange order execution")
    parser.add_argument("--exchange", required=True, choices=["binance", "okx", "bybit", "bitget", "mexc"])
    parser.add_argument("--symbol", default="SOLUSDT", help="Symbol to test (default: SOLUSDT)")
    parser.add_argument("--notional", type=float, default=10.0, help="Notional in USDT (default: 10)")
    parser.add_argument("--leverage", type=int, default=1, help="Leverage (default: 1)")
    args = parser.parse_args()

    asyncio.run(run_test(args.exchange, args.symbol, args.notional, args.leverage))


if __name__ == "__main__":
    main()
