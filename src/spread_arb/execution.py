"""Execution service - orchestrates spread entry/exit across two exchanges."""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from .config import Settings
from .exchanges.base import ExchangeClient
from .models import BalanceInfo, ExchangeName, Quote, SpreadOrderResult


class ExecutionError(Exception):
    """Raised when order execution fails."""


class ExecutionService:
    """Stateless executor for spread trades."""

    def __init__(
        self,
        settings: Settings,
        clients: dict[ExchangeName, ExchangeClient],
    ) -> None:
        self.settings = settings
        self.clients = clients
        self.log = logging.getLogger(__name__)
        self._min_qty_cache: dict[tuple[ExchangeName, str], Decimal] = {}
        self._balance_cache: dict[ExchangeName, tuple[float, float]] = {}
        self._balance_cache_ttl: float = 30.0

    async def initialize(self, symbols: list[str]) -> None:
        """Set leverage on all exchanges for all symbols. Call once at startup."""
        leverage = self.settings.default_leverage
        for name, client in self.clients.items():
            for symbol in symbols:
                try:
                    await client.set_leverage(symbol, leverage)
                    self.log.info("set leverage %dx on %s for %s", leverage, name.value, symbol)
                except NotImplementedError:
                    self.log.debug("leverage not supported on %s", name.value)
                except Exception as exc:
                    # Some exchanges error if leverage is already set - ignore.
                    self.log.warning("set_leverage failed on %s %s: %s", name.value, symbol, exc)

    async def get_balance(self, exchange: ExchangeName) -> BalanceInfo:
        return await self.clients[exchange].get_balance()

    async def _get_cached_balance(self, exchange: ExchangeName) -> float:
        """Get available balance with short cache to avoid hammering API."""
        now = time.time()
        cached = self._balance_cache.get(exchange)
        if cached and (now - cached[1]) < self._balance_cache_ttl:
            return cached[0]

        balance = await self.clients[exchange].get_balance()
        available = float(balance.available_usdt)
        self._balance_cache[exchange] = (available, now)
        return available

    def invalidate_balance_cache(self, exchange: ExchangeName) -> None:
        """Invalidate cached balance after fills."""
        self._balance_cache.pop(exchange, None)

    async def calculate_notional(
        self,
        long_exchange: ExchangeName,
        short_exchange: ExchangeName,
    ) -> float:
        """Calculate notional per leg from available balances on both exchanges."""
        if not self.settings.mr_compound_enabled:
            return self.settings.mr_notional_usdt

        try:
            long_avail = await self._get_cached_balance(long_exchange)
            short_avail = await self._get_cached_balance(short_exchange)
        except Exception as exc:
            self.log.warning("balance fetch failed for notional calc: %s - using fixed notional", exc)
            return self.settings.mr_notional_usdt

        min_available = min(long_avail, short_avail)
        notional = min_available * self.settings.mr_notional_pct / 100.0

        if notional < self.settings.mr_min_notional_usdt:
            self.log.warning(
                "compound notional $%.2f below minimum $%.2f - skipping trade",
                notional,
                self.settings.mr_min_notional_usdt,
            )
            return 0.0

        if notional > self.settings.max_notional_usdt:
            notional = self.settings.max_notional_usdt

        self.log.info(
            "compound notional: $%.2f (%.0f%% of min balance $%.2f)",
            notional,
            self.settings.mr_notional_pct,
            min_available,
        )
        return notional

    async def execute_spread_entry(
        self,
        symbol: str,
        long_exchange: ExchangeName,
        short_exchange: ExchangeName,
        notional_usdt: float,
        long_quote: Quote,
        short_quote: Quote,
    ) -> SpreadOrderResult:
        """Execute spread entry: buy on long_exchange, sell on short_exchange."""
        if notional_usdt > self.settings.max_notional_usdt:
            raise ExecutionError(
                f"notional {notional_usdt} exceeds max {self.settings.max_notional_usdt}"
            )

        long_client = self.clients[long_exchange]
        short_client = self.clients[short_exchange]

        long_price = float(long_quote.best_ask_price)
        short_price = float(short_quote.best_bid_price)
        long_qty = Decimal(str(notional_usdt)) / Decimal(str(long_price))
        short_qty = Decimal(str(notional_usdt)) / Decimal(str(short_price))

        await self._validate_min_qty(long_exchange, symbol, long_qty)
        await self._validate_min_qty(short_exchange, symbol, short_qty)

        try:
            long_result, short_result = await asyncio.wait_for(
                asyncio.gather(
                    long_client.place_market_order(symbol, "buy", long_qty),
                    short_client.place_market_order(symbol, "sell", short_qty),
                    return_exceptions=True,
                ),
                timeout=self.settings.order_timeout_sec,
            )
        except asyncio.TimeoutError as exc:
            raise ExecutionError(f"order timeout after {self.settings.order_timeout_sec}s") from exc

        if isinstance(long_result, Exception) and isinstance(short_result, Exception):
            raise ExecutionError(f"both legs failed: long={long_result}, short={short_result}")

        if isinstance(long_result, Exception):
            self.log.critical("LONG LEG FAILED, closing short | %s | %s", symbol, long_result)
            if not isinstance(short_result, Exception):
                try:
                    await short_client.place_market_order(symbol, "buy", short_result.filled_qty, close=True)
                except Exception as close_exc:
                    self.log.critical("FAILED TO CLOSE SHORT LEG | %s | %s", symbol, close_exc)
            raise ExecutionError(f"long leg failed: {long_result}")

        if isinstance(short_result, Exception):
            self.log.critical("SHORT LEG FAILED, closing long | %s | %s", symbol, short_result)
            if not isinstance(long_result, Exception):
                try:
                    await long_client.place_market_order(symbol, "sell", long_result.filled_qty, close=True)
                except Exception as close_exc:
                    self.log.critical("FAILED TO CLOSE LONG LEG | %s | %s", symbol, close_exc)
            raise ExecutionError(f"short leg failed: {short_result}")

        self.log.info(
            "spread entry filled | %s | long %s @ %s | short %s @ %s",
            symbol,
            long_exchange.value,
            long_result.avg_price,
            short_exchange.value,
            short_result.avg_price,
        )
        self.invalidate_balance_cache(long_exchange)
        self.invalidate_balance_cache(short_exchange)
        return SpreadOrderResult(long_order=long_result, short_order=short_result)

    async def execute_spread_exit(
        self,
        symbol: str,
        long_exchange: ExchangeName,
        short_exchange: ExchangeName,
        long_qty: Decimal,
        short_qty: Decimal,
    ) -> SpreadOrderResult:
        """Execute spread exit: sell long, buy back short."""
        long_client = self.clients[long_exchange]
        short_client = self.clients[short_exchange]

        try:
            long_result, short_result = await asyncio.wait_for(
                asyncio.gather(
                    long_client.place_market_order(symbol, "sell", long_qty, close=True),
                    short_client.place_market_order(symbol, "buy", short_qty, close=True),
                    return_exceptions=True,
                ),
                timeout=self.settings.order_timeout_sec,
            )
        except asyncio.TimeoutError as exc:
            self.log.critical("EXIT TIMEOUT | %s | manual intervention needed", symbol)
            raise ExecutionError(f"exit timeout after {self.settings.order_timeout_sec}s") from exc

        if isinstance(long_result, Exception):
            self.log.critical("EXIT LONG LEG FAILED | %s | %s - MANUAL CLOSE NEEDED", symbol, long_result)
        if isinstance(short_result, Exception):
            self.log.critical("EXIT SHORT LEG FAILED | %s | %s - MANUAL CLOSE NEEDED", symbol, short_result)
        if isinstance(long_result, Exception) or isinstance(short_result, Exception):
            raise ExecutionError(f"exit partially failed: long={long_result}, short={short_result}")

        self.log.info(
            "spread exit filled | %s | sell long %s @ %s | buy short %s @ %s",
            symbol,
            long_exchange.value,
            long_result.avg_price,
            short_exchange.value,
            short_result.avg_price,
        )
        self.invalidate_balance_cache(long_exchange)
        self.invalidate_balance_cache(short_exchange)
        return SpreadOrderResult(long_order=long_result, short_order=short_result)

    async def place_protective_stops(
        self,
        symbol: str,
        long_exchange: ExchangeName,
        short_exchange: ExchangeName,
        long_qty: Decimal,
        short_qty: Decimal,
        long_entry_price: Decimal,
        short_entry_price: Decimal,
    ) -> tuple[str, str]:
        """Place exchange-side catastrophic stop-market orders on both legs."""
        stop_pct = self.settings.exchange_stop_loss_pct / 100.0
        long_stop_price = long_entry_price * Decimal(str(1.0 - stop_pct))
        short_stop_price = short_entry_price * Decimal(str(1.0 + stop_pct))

        long_stop_id = ""
        short_stop_id = ""

        try:
            long_stop_id = await self.clients[long_exchange].place_stop_market_order(
                symbol=symbol,
                side="sell",
                qty=long_qty,
                stop_price=long_stop_price,
            )
            self.log.info(
                "protective stop placed | %s | LONG %s | stop @ %s (-%s%%)",
                symbol,
                long_exchange.value,
                long_stop_price,
                self.settings.exchange_stop_loss_pct,
            )
        except Exception as exc:
            self.log.error(
                "failed to place long protective stop | %s | %s | %s",
                symbol,
                long_exchange.value,
                exc,
            )

        try:
            short_stop_id = await self.clients[short_exchange].place_stop_market_order(
                symbol=symbol,
                side="buy",
                qty=short_qty,
                stop_price=short_stop_price,
            )
            self.log.info(
                "protective stop placed | %s | SHORT %s | stop @ %s (+%s%%)",
                symbol,
                short_exchange.value,
                short_stop_price,
                self.settings.exchange_stop_loss_pct,
            )
        except Exception as exc:
            self.log.error(
                "failed to place short protective stop | %s | %s | %s",
                symbol,
                short_exchange.value,
                exc,
            )

        return long_stop_id, short_stop_id

    async def cancel_protective_stops(
        self,
        symbol: str,
        long_exchange: ExchangeName,
        short_exchange: ExchangeName,
        long_stop_id: str,
        short_stop_id: str,
    ) -> None:
        """Cancel protective stops before normal bot-driven exit."""
        if long_stop_id:
            try:
                await self.clients[long_exchange].cancel_order(symbol, long_stop_id)
                self.log.debug("cancelled long protective stop | %s | %s", symbol, long_exchange.value)
            except Exception as exc:
                self.log.warning("failed to cancel long stop | %s | %s | %s", symbol, long_exchange.value, exc)

        if short_stop_id:
            try:
                await self.clients[short_exchange].cancel_order(symbol, short_stop_id)
                self.log.debug("cancelled short protective stop | %s | %s", symbol, short_exchange.value)
            except Exception as exc:
                self.log.warning("failed to cancel short stop | %s | %s | %s", symbol, short_exchange.value, exc)

    async def _validate_min_qty(self, exchange: ExchangeName, symbol: str, qty: Decimal) -> None:
        cache_key = (exchange, symbol)
        if cache_key not in self._min_qty_cache:
            try:
                min_qty = await self.clients[exchange].get_min_order_qty(symbol)
                self._min_qty_cache[cache_key] = min_qty
            except NotImplementedError:
                self._min_qty_cache[cache_key] = Decimal("0")
            except Exception as exc:
                self.log.warning("failed to get min qty for %s %s: %s", exchange.value, symbol, exc)
                self._min_qty_cache[cache_key] = Decimal("0")

        min_qty = self._min_qty_cache[cache_key]
        if qty < min_qty:
            raise ExecutionError(
                f"qty {qty} below minimum {min_qty} on {exchange.value} for {symbol}"
            )
