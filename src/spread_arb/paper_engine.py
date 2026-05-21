from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Callable

from .config import Settings
from .models import ExchangeName, Quote
from .opportunity import SpreadOpportunity, classify_opportunity
from .storage import OpportunityStore, PaperTradeRecord

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CloseDecision:
    should_close: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class PnlResult:
    gross_pnl_usdt: float
    fees_usdt: float
    slippage_usdt: float
    funding_usdt: float
    net_pnl_usdt: float
    net_pnl_pct: float


@dataclass(slots=True)
class PendingEntry:
    key: tuple[str, ExchangeName, ExchangeName]
    opportunity: SpreadOpportunity
    planned_at: datetime
    task: asyncio.Task[None]


@dataclass(slots=True)
class OpenPaperPosition:
    symbol: str
    long_exchange: ExchangeName
    short_exchange: ExchangeName
    notional_usdt: float
    opened_at: datetime
    entry_long_price: float
    entry_short_price: float
    entry_raw_spread_pct: float
    entry_net_spread_pct: float
    estimated_entry_fees_usdt: float
    estimated_entry_slippage_usdt: float
    max_adverse_spread_pct: float
    max_favorable_spread_pct: float

    @property
    def key(self) -> tuple[str, ExchangeName, ExchangeName]:
        return (self.symbol, self.long_exchange, self.short_exchange)


def decide_close_reason(
    *,
    current_raw_spread_pct: float,
    hold_seconds: float,
    has_fresh_quotes: bool,
    exit_spread_pct: float,
    stop_spread_pct: float,
    max_hold_seconds: int,
) -> CloseDecision:
    if not has_fresh_quotes:
        return CloseDecision(should_close=True, reason="stale_or_missing_quote")
    if current_raw_spread_pct >= stop_spread_pct:
        return CloseDecision(should_close=True, reason="stop_spread")
    if current_raw_spread_pct <= exit_spread_pct:
        return CloseDecision(should_close=True, reason="spread_converged")
    if hold_seconds >= max_hold_seconds:
        return CloseDecision(should_close=True, reason="max_hold_time")
    return CloseDecision(should_close=False, reason=None)


def calculate_pnl(
    *,
    notional_usdt: float,
    entry_long_price: float,
    entry_short_price: float,
    exit_long_price: float,
    exit_short_price: float,
    entry_fees_usdt: float,
    exit_fees_usdt: float,
    entry_slippage_usdt: float,
    exit_slippage_usdt: float,
) -> PnlResult:
    # Guard against zero/missing prices from exchange API failures.
    # Fall back to entry price (= zero PnL on that leg) rather than
    # recording a catastrophic fake loss.
    if exit_long_price <= 0:
        _log.warning("exit_long_price is %.8f — using entry price %.8f as fallback", exit_long_price, entry_long_price)
        exit_long_price = entry_long_price
    if exit_short_price <= 0:
        _log.warning("exit_short_price is %.8f — using entry price %.8f as fallback", exit_short_price, entry_short_price)
        exit_short_price = entry_short_price

    long_pnl = notional_usdt * (exit_long_price - entry_long_price) / entry_long_price
    short_pnl = notional_usdt * (entry_short_price - exit_short_price) / entry_short_price
    gross_pnl_usdt = long_pnl + short_pnl

    fees_usdt = entry_fees_usdt + exit_fees_usdt
    slippage_usdt = entry_slippage_usdt + exit_slippage_usdt
    funding_usdt = 0.0
    net_pnl_usdt = gross_pnl_usdt - fees_usdt - slippage_usdt - funding_usdt

    # Basis: total exposure = 2 * per-leg notional.
    total_exposure = 2.0 * notional_usdt
    net_pnl_pct = (net_pnl_usdt / total_exposure) * 100.0

    return PnlResult(
        gross_pnl_usdt=gross_pnl_usdt,
        fees_usdt=fees_usdt,
        slippage_usdt=slippage_usdt,
        funding_usdt=funding_usdt,
        net_pnl_usdt=net_pnl_usdt,
        net_pnl_pct=net_pnl_pct,
    )


class PaperEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        opportunity_store: OpportunityStore,
        get_latest_quote: Callable[[ExchangeName, str], Quote | None],
    ) -> None:
        self.settings = settings
        self.opportunity_store = opportunity_store
        self.get_latest_quote = get_latest_quote
        self.log = logging.getLogger(__name__)

        self.pending_entries: dict[tuple[str, ExchangeName, ExchangeName], PendingEntry] = {}
        self.open_positions: dict[tuple[str, ExchangeName, ExchangeName], OpenPaperPosition] = {}
        self.last_closed_by_symbol: dict[str, datetime] = {}

        self.closed_trades_count = 0
        self.winning_trades_count = 0
        self.total_net_pnl_usdt = 0.0

        self.exchange_fees_pct: dict[ExchangeName, float] = {
            ExchangeName.MEXC: self.settings.taker_fee_mexc_pct,
            ExchangeName.BYBIT: self.settings.taker_fee_bybit_pct,
        }

    async def shutdown(self) -> None:
        tasks = [pending.task for pending in self.pending_entries.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.pending_entries.clear()

    def on_observed_opportunity(self, opportunity: SpreadOpportunity) -> None:
        key = (opportunity.symbol, opportunity.long_exchange, opportunity.short_exchange)
        now = datetime.now(UTC)

        if key in self.pending_entries or key in self.open_positions:
            return
        if len(self.open_positions) >= self.settings.max_open_positions:
            return
        if self.settings.one_position_per_symbol and any(pos.symbol == opportunity.symbol for pos in self.open_positions.values()):
            return

        last_closed_at = self.last_closed_by_symbol.get(opportunity.symbol)
        if last_closed_at is not None:
            cooldown = timedelta(seconds=self.settings.symbol_cooldown_sec)
            if now - last_closed_at < cooldown:
                return

        planned_at = now + timedelta(milliseconds=self.settings.simulated_execution_delay_ms)
        task = asyncio.create_task(self._execute_after_delay(key), name=f"paper-entry-{opportunity.symbol}")
        self.pending_entries[key] = PendingEntry(
            key=key,
            opportunity=opportunity,
            planned_at=planned_at,
            task=task,
        )

    def on_quote_tick(self) -> None:
        if not self.open_positions:
            return

        now = datetime.now(UTC)
        max_age_ms = self.settings.max_quote_age_ms

        for key, position in list(self.open_positions.items()):
            long_quote = self.get_latest_quote(position.long_exchange, position.symbol)
            short_quote = self.get_latest_quote(position.short_exchange, position.symbol)

            if long_quote is None or short_quote is None:
                self._close_position(position, close_reason="stale_or_missing_quote", long_quote=None, short_quote=None)
                continue

            age_long_ms = (now - long_quote.received_at).total_seconds() * 1000.0
            age_short_ms = (now - short_quote.received_at).total_seconds() * 1000.0
            is_fresh = age_long_ms <= max_age_ms and age_short_ms <= max_age_ms

            current_raw_spread_pct = _raw_spread_pct(ask_long=long_quote.best_ask_price, bid_short=short_quote.best_bid_price)
            position.max_adverse_spread_pct = min(position.max_adverse_spread_pct, current_raw_spread_pct)
            position.max_favorable_spread_pct = max(position.max_favorable_spread_pct, current_raw_spread_pct)

            hold_seconds = (now - position.opened_at).total_seconds()
            decision = decide_close_reason(
                current_raw_spread_pct=current_raw_spread_pct,
                hold_seconds=hold_seconds,
                has_fresh_quotes=is_fresh,
                exit_spread_pct=self.settings.exit_spread_pct,
                stop_spread_pct=self.settings.stop_spread_pct,
                max_hold_seconds=self.settings.max_hold_seconds,
            )
            if decision.should_close:
                self._close_position(position, close_reason=decision.reason or "max_hold_time", long_quote=long_quote, short_quote=short_quote)

    async def summary_loop(self, stop_event: asyncio.Event) -> None:
        interval_sec = 10.0
        while not stop_event.is_set():
            winrate = (self.winning_trades_count / self.closed_trades_count * 100.0) if self.closed_trades_count else 0.0
            self.log.info(
                "paper summary | open=%d | closed=%d | total_net_pnl=%.4f USDT | winrate=%.2f%%",
                len(self.open_positions),
                self.closed_trades_count,
                self.total_net_pnl_usdt,
                winrate,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
            except TimeoutError:
                continue

    async def _execute_after_delay(self, key: tuple[str, ExchangeName, ExchangeName]) -> None:
        pending = self.pending_entries.get(key)
        if pending is None:
            return

        delay_sec = self.settings.simulated_execution_delay_ms / 1000.0
        try:
            await asyncio.sleep(delay_sec)
            current = self.pending_entries.get(key)
            if current is None:
                return
            opportunity = current.opportunity

            long_quote = self.get_latest_quote(opportunity.long_exchange, opportunity.symbol)
            short_quote = self.get_latest_quote(opportunity.short_exchange, opportunity.symbol)
            if long_quote is None or short_quote is None:
                self._mark_opportunity_missed(opportunity, reason="stale_or_missing_quote_after_delay")
                return

            now = datetime.now(UTC)
            quote_age_ms_long = (now - long_quote.received_at).total_seconds() * 1000.0
            quote_age_ms_short = (now - short_quote.received_at).total_seconds() * 1000.0

            raw_spread_pct = _raw_spread_pct(ask_long=long_quote.best_ask_price, bid_short=short_quote.best_bid_price)
            if raw_spread_pct < self.settings.min_raw_spread_pct:
                self._mark_opportunity_missed(opportunity, reason="spread_disappeared_after_delay")
                return

            roundtrip_cost_pct = _roundtrip_cost_pct(
                fee_long_pct=self.exchange_fees_pct.get(opportunity.long_exchange, 0.0),
                fee_short_pct=self.exchange_fees_pct.get(opportunity.short_exchange, 0.0),
                slippage_buffer_pct=self.settings.slippage_buffer_pct,
                safety_buffer_pct=self.settings.safety_buffer_pct,
            )
            net_spread_pct = raw_spread_pct - roundtrip_cost_pct

            long_notional_capacity = float(long_quote.best_ask_price * long_quote.best_ask_size)
            short_notional_capacity = float(short_quote.best_bid_price * short_quote.best_bid_size)
            is_liquid = (
                long_notional_capacity >= self.settings.paper_notional_usdt
                and short_notional_capacity >= self.settings.paper_notional_usdt
            )

            is_fresh = quote_age_ms_long <= self.settings.max_quote_age_ms and quote_age_ms_short <= self.settings.max_quote_age_ms
            decision = classify_opportunity(
                estimated_net_spread_pct=net_spread_pct,
                entry_net_spread_pct=self.settings.entry_net_spread_pct,
                is_fresh=is_fresh,
                is_liquid=is_liquid,
            )
            if decision.status != "observed":
                self._mark_opportunity_missed(opportunity, reason=f"{decision.reason}_after_delay")
                return

            if len(self.open_positions) >= self.settings.max_open_positions:
                self._mark_opportunity_missed(opportunity, reason="max_open_positions_reached")
                return
            if self.settings.one_position_per_symbol and any(pos.symbol == opportunity.symbol for pos in self.open_positions.values()):
                self._mark_opportunity_missed(opportunity, reason="symbol_already_open")
                return

            last_closed_at = self.last_closed_by_symbol.get(opportunity.symbol)
            if last_closed_at is not None:
                cooldown = timedelta(seconds=self.settings.symbol_cooldown_sec)
                if now - last_closed_at < cooldown:
                    self._mark_opportunity_missed(opportunity, reason="symbol_cooldown")
                    return

            entry_fees_usdt = _one_side_fees_usdt(
                notional_usdt=self.settings.paper_notional_usdt,
                fee_long_pct=self.exchange_fees_pct.get(opportunity.long_exchange, 0.0),
                fee_short_pct=self.exchange_fees_pct.get(opportunity.short_exchange, 0.0),
            )
            entry_slippage_usdt = _one_side_slippage_usdt(
                notional_usdt=self.settings.paper_notional_usdt,
                slippage_buffer_pct=self.settings.slippage_buffer_pct,
            )

            position = OpenPaperPosition(
                symbol=opportunity.symbol,
                long_exchange=opportunity.long_exchange,
                short_exchange=opportunity.short_exchange,
                notional_usdt=self.settings.paper_notional_usdt,
                opened_at=now,
                entry_long_price=float(long_quote.best_ask_price),
                entry_short_price=float(short_quote.best_bid_price),
                entry_raw_spread_pct=raw_spread_pct,
                entry_net_spread_pct=net_spread_pct,
                estimated_entry_fees_usdt=entry_fees_usdt,
                estimated_entry_slippage_usdt=entry_slippage_usdt,
                max_adverse_spread_pct=raw_spread_pct,
                max_favorable_spread_pct=raw_spread_pct,
            )
            self.open_positions[position.key] = position

            if opportunity.opportunity_id is not None:
                self.opportunity_store.update_opportunity_status(opportunity.opportunity_id, "opened", opportunity.reason)

            self.log.info(
                "paper open | %s | long=%s @ %.6f | short=%s @ %.6f | raw=%.4f%% | net=%.4f%%",
                position.symbol,
                position.long_exchange.value,
                position.entry_long_price,
                position.short_exchange.value,
                position.entry_short_price,
                position.entry_raw_spread_pct,
                position.entry_net_spread_pct,
            )
        except asyncio.CancelledError:
            raise
        finally:
            self.pending_entries.pop(key, None)

    def _mark_opportunity_missed(self, opportunity: SpreadOpportunity, reason: str) -> None:
        if opportunity.opportunity_id is not None:
            self.opportunity_store.update_opportunity_status(opportunity.opportunity_id, "missed", reason)
        self.log.info(
            "paper missed | %s | long=%s short=%s | reason=%s",
            opportunity.symbol,
            opportunity.long_exchange.value,
            opportunity.short_exchange.value,
            reason,
        )

    def _close_position(
        self,
        position: OpenPaperPosition,
        *,
        close_reason: str,
        long_quote: Quote | None,
        short_quote: Quote | None,
    ) -> None:
        now = datetime.now(UTC)

        if long_quote is None or short_quote is None:
            exit_long_price = position.entry_long_price
            exit_short_price = position.entry_short_price
            exit_raw_spread_pct = position.entry_raw_spread_pct
        else:
            exit_long_price = float(long_quote.best_bid_price)
            exit_short_price = float(short_quote.best_ask_price)
            exit_raw_spread_pct = _raw_spread_pct(ask_long=long_quote.best_ask_price, bid_short=short_quote.best_bid_price)

        exit_fees_usdt = _one_side_fees_usdt(
            notional_usdt=position.notional_usdt,
            fee_long_pct=self.exchange_fees_pct.get(position.long_exchange, 0.0),
            fee_short_pct=self.exchange_fees_pct.get(position.short_exchange, 0.0),
        )
        exit_slippage_usdt = _one_side_slippage_usdt(
            notional_usdt=position.notional_usdt,
            slippage_buffer_pct=self.settings.slippage_buffer_pct,
        )
        pnl = calculate_pnl(
            notional_usdt=position.notional_usdt,
            entry_long_price=position.entry_long_price,
            entry_short_price=position.entry_short_price,
            exit_long_price=exit_long_price,
            exit_short_price=exit_short_price,
            entry_fees_usdt=position.estimated_entry_fees_usdt,
            exit_fees_usdt=exit_fees_usdt,
            entry_slippage_usdt=position.estimated_entry_slippage_usdt,
            exit_slippage_usdt=exit_slippage_usdt,
        )

        hold_seconds = (now - position.opened_at).total_seconds()
        self.opportunity_store.insert_paper_trade(
            PaperTradeRecord(
                symbol=position.symbol,
                long_exchange=position.long_exchange.value,
                short_exchange=position.short_exchange.value,
                notional_usdt=position.notional_usdt,
                opened_at=position.opened_at.isoformat(),
                closed_at=now.isoformat(),
                hold_seconds=hold_seconds,
                entry_long_price=position.entry_long_price,
                entry_short_price=position.entry_short_price,
                exit_long_price=exit_long_price,
                exit_short_price=exit_short_price,
                entry_raw_spread_pct=position.entry_raw_spread_pct,
                exit_raw_spread_pct=exit_raw_spread_pct,
                gross_pnl_usdt=pnl.gross_pnl_usdt,
                fees_usdt=pnl.fees_usdt,
                slippage_usdt=pnl.slippage_usdt,
                funding_usdt=pnl.funding_usdt,
                net_pnl_usdt=pnl.net_pnl_usdt,
                net_pnl_pct=pnl.net_pnl_pct,
                close_reason=close_reason,
                max_adverse_spread_pct=position.max_adverse_spread_pct,
                max_favorable_spread_pct=position.max_favorable_spread_pct,
                created_at=OpportunityStore.now_iso(),
            )
        )

        self.closed_trades_count += 1
        if pnl.net_pnl_usdt > 0:
            self.winning_trades_count += 1
        self.total_net_pnl_usdt += pnl.net_pnl_usdt
        self.last_closed_by_symbol[position.symbol] = now
        self.open_positions.pop(position.key, None)

        self.log.info(
            "paper close | %s | reason=%s | hold=%.1fs | net_pnl=%.4f USDT (%.4f%%) | gross=%.4f | fees=%.4f | slippage=%.4f",
            position.symbol,
            close_reason,
            hold_seconds,
            pnl.net_pnl_usdt,
            pnl.net_pnl_pct,
            pnl.gross_pnl_usdt,
            pnl.fees_usdt,
            pnl.slippage_usdt,
        )


def _raw_spread_pct(*, ask_long: Decimal, bid_short: Decimal) -> float:
    return float((bid_short - ask_long) / ask_long * Decimal("100"))


def _roundtrip_cost_pct(
    *,
    fee_long_pct: float,
    fee_short_pct: float,
    slippage_buffer_pct: float,
    safety_buffer_pct: float,
) -> float:
    return (2.0 * fee_long_pct) + (2.0 * fee_short_pct) + slippage_buffer_pct + safety_buffer_pct


def _one_side_fees_usdt(*, notional_usdt: float, fee_long_pct: float, fee_short_pct: float) -> float:
    return notional_usdt * (fee_long_pct + fee_short_pct) / 100.0


def _one_side_slippage_usdt(*, notional_usdt: float, slippage_buffer_pct: float) -> float:
    # Roundtrip slippage is modeled as SLIPPAGE_BUFFER_PCT over total exposure (2 * notional).
    roundtrip_slippage = (2.0 * notional_usdt) * (slippage_buffer_pct / 100.0)
    return roundtrip_slippage / 2.0
