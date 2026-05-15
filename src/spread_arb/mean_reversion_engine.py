from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Callable

from .config import Settings
from .models import ExchangeName, Quote
from .paper_engine import calculate_pnl
from .storage import OpportunityStore, PaperTradeRecord


@dataclass(slots=True)
class RollingBaseline:
    """Rolling mean and std for a specific directional pair."""

    window: deque[float]
    window_size: int
    sum_x: float = 0.0
    sum_x2: float = 0.0

    @property
    def mean(self) -> float:
        n = len(self.window)
        if n == 0:
            return 0.0
        return self.sum_x / n

    @property
    def std(self) -> float:
        n = len(self.window)
        if n <= 1:
            return 0.0
        mean = self.mean
        variance = (self.sum_x2 / n) - (mean * mean)
        return math.sqrt(max(variance, 0.0))

    @property
    def is_ready(self) -> bool:
        """Need at least window_size samples before generating signals."""
        return len(self.window) >= self.window_size

    def update(self, value: float) -> None:
        """Add new spread value, remove oldest if window is full."""
        if len(self.window) >= self.window_size:
            oldest = self.window.popleft()
            self.sum_x -= oldest
            self.sum_x2 -= oldest * oldest
        self.window.append(value)
        self.sum_x += value
        self.sum_x2 += value * value


@dataclass(slots=True)
class MeanRevPosition:
    """An open mean reversion paper position."""

    symbol: str
    long_exchange: ExchangeName
    short_exchange: ExchangeName
    direction: str
    notional_usdt: float
    opened_at: datetime
    entry_long_price: float
    entry_short_price: float
    entry_spread_pct: float
    entry_rolling_mean: float
    entry_rolling_std: float
    sigma_at_entry: float
    take_profit_target: float
    max_adverse_spread_pct: float
    max_favorable_spread_pct: float
    estimated_entry_fees_usdt: float
    estimated_entry_slippage_usdt: float


@dataclass(slots=True)
class PendingMrEntry:
    symbol: str
    long_exchange: ExchangeName
    short_exchange: ExchangeName
    direction: str
    signal_spread_pct: float
    rolling_mean: float
    rolling_std: float
    sigma_at_signal: float
    expected_edge_pct: float
    net_edge_pct: float
    planned_at: datetime
    task: asyncio.Task[None]

    @property
    def key(self) -> tuple[str, ExchangeName, ExchangeName]:
        return (self.symbol, self.long_exchange, self.short_exchange)


class MeanReversionEngine:
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

        self.baselines: dict[tuple[str, ExchangeName, ExchangeName], RollingBaseline] = {}
        self.baseline_ready_keys: set[tuple[str, ExchangeName, ExchangeName]] = set()
        self._preloading = False

        # Quote freshness tracker: per (exchange, symbol) rolling window of booleans
        # True = quote was fresh (< max_quote_age_ms) at baseline check time
        self._freshness_window_size = self.settings.mr_quote_freshness_window
        self._freshness: dict[tuple[ExchangeName, str], deque[bool]] = {}
        self._min_freshness_pct = self.settings.mr_min_quote_freshness_pct

        self.pending_entries_by_symbol: dict[str, PendingMrEntry] = {}
        self.open_positions_by_symbol: dict[str, MeanRevPosition] = {}
        self.last_closed_by_symbol: dict[str, datetime] = {}

        self.closed_trades_count = 0
        self.winning_trades_count = 0
        self.total_net_pnl_usdt = 0.0
        self.total_hold_seconds = 0.0
        self.close_reason_counts: Counter[str] = Counter()
        self.symbol_pnl_usdt: defaultdict[str, float] = defaultdict(float)
        self.max_baseline_quote_age_ms = 30_000.0

        self.exchange_fees_pct: dict[ExchangeName, float] = {
            ExchangeName.MEXC: self.settings.taker_fee_mexc_pct,
            ExchangeName.BYBIT: self.settings.taker_fee_bybit_pct,
            ExchangeName.OKX: self.settings.taker_fee_okx_pct,
            ExchangeName.BINANCE: self.settings.taker_fee_binance_pct,
            ExchangeName.GATE: self.settings.taker_fee_gate_pct,
            ExchangeName.BITGET: self.settings.taker_fee_bitget_pct,
            ExchangeName.HTX: self.settings.taker_fee_htx_pct,
        }

    def preload_baselines(self, database_url: str) -> None:
        """Load recent spread snapshots from DB to pre-populate rolling baselines."""
        import sqlite3

        from .storage import _sqlite_path_from_url

        db_path = _sqlite_path_from_url(database_url)
        if not db_path.exists():
            self.log.warning("preload: database not found at %s", db_path)
            return

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        self._preloading = True
        try:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='spread_snapshots' LIMIT 1"
            ).fetchone()
            if has_table is None:
                self.log.info("preload: spread_snapshots table not found, starting cold")
                return

            window = self.settings.mr_rolling_window
            max_rows = window * 700
            rows = conn.execute(
                """
                SELECT
                    symbol,
                    exchange_a,
                    exchange_b,
                    raw_spread_ab_pct,
                    raw_spread_ba_pct
                FROM spread_snapshots
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (max_rows,),
            ).fetchall()

            if not rows:
                self.log.info("preload: no snapshots found, starting cold")
                return

            loaded_count = 0
            for row in reversed(rows):
                symbol = str(row["symbol"])
                ex_a_raw = str(row["exchange_a"])
                ex_b_raw = str(row["exchange_b"])
                try:
                    ex_a = ExchangeName(ex_a_raw)
                    ex_b = ExchangeName(ex_b_raw)
                except ValueError:
                    continue

                self._update_baseline(
                    symbol=symbol,
                    long_exchange=ex_a,
                    short_exchange=ex_b,
                    spread_pct=float(row["raw_spread_ab_pct"]),
                )
                self._update_baseline(
                    symbol=symbol,
                    long_exchange=ex_b,
                    short_exchange=ex_a,
                    spread_pct=float(row["raw_spread_ba_pct"]),
                )
                loaded_count += 1
        finally:
            self._preloading = False
            conn.close()

        ready_count = len(self.baseline_ready_keys)
        total_baselines = len(self.baselines)
        self.log.info(
            "preload complete | loaded %d snapshots | %d baselines total | %d ready",
            loaded_count,
            total_baselines,
            ready_count,
        )

    async def shutdown(self) -> None:
        tasks = [pending.task for pending in self.pending_entries_by_symbol.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.pending_entries_by_symbol.clear()
        self.log_summary()

    async def summary_loop(self, stop_event: asyncio.Event) -> None:
        interval_sec = 60.0
        while not stop_event.is_set():
            self.log_summary()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
            except TimeoutError:
                continue

    def log_summary(self) -> None:
        avg_hold = self.total_hold_seconds / self.closed_trades_count if self.closed_trades_count else 0.0
        avg_pnl = self.total_net_pnl_usdt / self.closed_trades_count if self.closed_trades_count else 0.0
        winrate = (self.winning_trades_count / self.closed_trades_count * 100.0) if self.closed_trades_count else 0.0

        reason_parts = [
            f"mean_reversion={self.close_reason_counts.get('mean_reversion', 0)}",
            f"stop_loss={self.close_reason_counts.get('stop_loss', 0)}",
            f"timeout={self.close_reason_counts.get('timeout', 0)}",
            f"stale_quote={self.close_reason_counts.get('stale_quote', 0)}",
        ]
        top_symbols = sorted(self.symbol_pnl_usdt.items(), key=lambda item: item[1], reverse=True)[:5]
        top_symbols_text = ", ".join(f"{symbol}:{pnl:+.2f}" for symbol, pnl in top_symbols) if top_symbols else "-"

        self.log.info(
            "mr summary | open=%d | closed=%d | wins=%d (%.1f%%) | total_pnl=%+.2f USDT | avg_hold=%.1fs | avg_pnl=%+.2f USDT | reasons=%s | top_symbols=%s",
            len(self.open_positions_by_symbol),
            self.closed_trades_count,
            self.winning_trades_count,
            winrate,
            self.total_net_pnl_usdt,
            avg_hold,
            avg_pnl,
            ", ".join(reason_parts),
            top_symbols_text,
        )

    def _update_freshness(self, exchange: ExchangeName, symbol: str, is_fresh: bool) -> None:
        """Track whether a quote was fresh at this baseline check."""
        key = (exchange, symbol)
        window = self._freshness.get(key)
        if window is None:
            window = deque(maxlen=self._freshness_window_size)
            self._freshness[key] = window
        window.append(is_fresh)

    def _get_freshness_pct(self, exchange: ExchangeName, symbol: str) -> float:
        """Return percentage of recent baseline checks where quote was fresh."""
        key = (exchange, symbol)
        window = self._freshness.get(key)
        if window is None or len(window) == 0:
            return 0.0
        return sum(window) / len(window) * 100.0

    def _freshness_ready(self, exchange: ExchangeName, symbol: str) -> bool:
        """Has enough freshness history to make a judgment."""
        key = (exchange, symbol)
        window = self._freshness.get(key)
        return window is not None and len(window) >= min(10, self._freshness_window_size)

    def update_baselines(self, snapshot_quotes: dict[tuple[ExchangeName, str], Quote]) -> None:
        now = datetime.now(UTC)
        quotes_by_symbol: dict[str, dict[ExchangeName, Quote]] = {}
        for (exchange, symbol), quote in snapshot_quotes.items():
            quotes_by_symbol.setdefault(symbol, {})[exchange] = quote

        # Update freshness tracker for all quotes
        freshness_threshold_ms = float(self.settings.max_quote_age_ms)
        for (exchange, symbol), quote in snapshot_quotes.items():
            age_ms = (now - quote.received_at).total_seconds() * 1000.0
            self._update_freshness(exchange, symbol, age_ms <= freshness_threshold_ms)

        for symbol, exchange_quotes in quotes_by_symbol.items():
            if len(exchange_quotes) < 2:
                continue

            exchanges = list(exchange_quotes.keys())
            for i in range(len(exchanges)):
                for j in range(i + 1, len(exchanges)):
                    ex_a = exchanges[i]
                    ex_b = exchanges[j]
                    q_a = exchange_quotes[ex_a]
                    q_b = exchange_quotes[ex_b]
                    age_a_ms = (now - q_a.received_at).total_seconds() * 1000.0
                    age_b_ms = (now - q_b.received_at).total_seconds() * 1000.0
                    if age_a_ms > self.max_baseline_quote_age_ms or age_b_ms > self.max_baseline_quote_age_ms:
                        continue

                    if q_a.best_ask_price <= Decimal("0") or q_b.best_ask_price <= Decimal("0"):
                        continue

                    spread_ab = _directional_spread_pct(ask_long=q_a.best_ask_price, bid_short=q_b.best_bid_price)
                    spread_ba = _directional_spread_pct(ask_long=q_b.best_ask_price, bid_short=q_a.best_bid_price)

                    self._evaluate_signal(
                        symbol=symbol,
                        long_exchange=ex_a,
                        short_exchange=ex_b,
                        long_quote=q_a,
                        short_quote=q_b,
                        spread_pct=spread_ab,
                        direction=_direction_tag(long_exchange=ex_a, short_exchange=ex_b),
                        now=now,
                    )
                    self._update_baseline(symbol=symbol, long_exchange=ex_a, short_exchange=ex_b, spread_pct=spread_ab)

                    self._evaluate_signal(
                        symbol=symbol,
                        long_exchange=ex_b,
                        short_exchange=ex_a,
                        long_quote=q_b,
                        short_quote=q_a,
                        spread_pct=spread_ba,
                        direction=_direction_tag(long_exchange=ex_b, short_exchange=ex_a),
                        now=now,
                    )
                    self._update_baseline(symbol=symbol, long_exchange=ex_b, short_exchange=ex_a, spread_pct=spread_ba)

    def check_exits(self, latest_quotes: dict[tuple[ExchangeName, str], Quote]) -> None:
        if not self.open_positions_by_symbol:
            return

        now = datetime.now(UTC)
        exit_max_age_ms = self.settings.mr_exit_max_quote_age_ms
        tracking_fresh_max_age_ms = min(exit_max_age_ms, 5_000)

        for symbol, position in list(self.open_positions_by_symbol.items()):
            long_quote = latest_quotes.get((position.long_exchange, symbol))
            short_quote = latest_quotes.get((position.short_exchange, symbol))

            if long_quote is None or short_quote is None:
                self._close_position(position=position, close_reason="stale_quote", long_quote=None, short_quote=None)
                continue

            age_long_ms = (now - long_quote.received_at).total_seconds() * 1000.0
            age_short_ms = (now - short_quote.received_at).total_seconds() * 1000.0

            current_spread_pct = _directional_spread_pct(ask_long=long_quote.best_ask_price, bid_short=short_quote.best_bid_price)
            if age_long_ms <= tracking_fresh_max_age_ms and age_short_ms <= tracking_fresh_max_age_ms:
                position.max_adverse_spread_pct = min(position.max_adverse_spread_pct, current_spread_pct)
                position.max_favorable_spread_pct = max(position.max_favorable_spread_pct, current_spread_pct)

            hold_seconds = (now - position.opened_at).total_seconds()
            stop_threshold = position.entry_rolling_mean + (self.settings.mr_sigma_stop * position.entry_rolling_std)

            close_reason: str | None = None
            if current_spread_pct <= position.take_profit_target:
                close_reason = "mean_reversion"
            elif current_spread_pct > stop_threshold:
                close_reason = "stop_loss"
            elif hold_seconds >= self.settings.mr_max_hold_seconds:
                close_reason = "timeout"
            elif age_long_ms > exit_max_age_ms or age_short_ms > exit_max_age_ms:
                close_reason = "stale_quote"

            if close_reason is not None:
                self._close_position(
                    position=position,
                    close_reason=close_reason,
                    long_quote=long_quote,
                    short_quote=short_quote,
                )

    def _update_baseline(
        self,
        *,
        symbol: str,
        long_exchange: ExchangeName,
        short_exchange: ExchangeName,
        spread_pct: float,
    ) -> None:
        key = (symbol, long_exchange, short_exchange)
        baseline = self.baselines.get(key)
        if baseline is None:
            baseline = RollingBaseline(window=deque(), window_size=self.settings.mr_rolling_window)
            self.baselines[key] = baseline

        was_ready = baseline.is_ready
        baseline.update(spread_pct)
        if not was_ready and baseline.is_ready and key not in self.baseline_ready_keys:
            self.baseline_ready_keys.add(key)
            if not self._preloading:
                self.log.info(
                    "mr baseline ready | %s %s->%s | window=%d | mean=%+.4f%% | std=%.4f%%",
                    symbol,
                    long_exchange.value,
                    short_exchange.value,
                    baseline.window_size,
                    baseline.mean,
                    baseline.std,
                )

    def _evaluate_signal(
        self,
        *,
        symbol: str,
        long_exchange: ExchangeName,
        short_exchange: ExchangeName,
        long_quote: Quote,
        short_quote: Quote,
        spread_pct: float,
        direction: str,
        now: datetime,
    ) -> None:
        excluded = set(self.settings.mr_excluded_exchanges)
        if long_exchange.value in excluded or short_exchange.value in excluded:
            return

        # Quote freshness filter: reject pairs where quotes are unreliable
        if self._freshness_ready(long_exchange, symbol) and self._freshness_ready(short_exchange, symbol):
            long_fresh = self._get_freshness_pct(long_exchange, symbol)
            short_fresh = self._get_freshness_pct(short_exchange, symbol)
            if long_fresh < self._min_freshness_pct or short_fresh < self._min_freshness_pct:
                return

        key = (symbol, long_exchange, short_exchange)
        baseline = self.baselines.get(key)
        if baseline is None or not baseline.is_ready:
            return

        mean = baseline.mean
        std = baseline.std
        threshold = mean + (self.settings.mr_sigma_entry * std)
        if spread_pct <= threshold:
            return

        sigma = _sigma_from(mean=mean, std=std, spread_pct=spread_pct)
        roundtrip_cost_pct = _roundtrip_cost_pct(
            fee_long_pct=self.exchange_fees_pct.get(long_exchange, 0.0),
            fee_short_pct=self.exchange_fees_pct.get(short_exchange, 0.0),
            slippage_buffer_pct=self.settings.slippage_buffer_pct,
            safety_buffer_pct=self.settings.safety_buffer_pct,
        )
        expected_edge_pct = spread_pct - mean
        net_edge_pct = expected_edge_pct - roundtrip_cost_pct

        long_fresh_pct = self._get_freshness_pct(long_exchange, symbol)
        short_fresh_pct = self._get_freshness_pct(short_exchange, symbol)
        self.log.info(
            "mr signal | %s %s->%s | spread=%+.4f%% | mean=%+.4f%% | std=%.4f%% | sigma=%.2f | net_edge=%+.4f%% | fresh=%.0f%%/%.0f%%",
            symbol,
            long_exchange.value,
            short_exchange.value,
            spread_pct,
            mean,
            std,
            sigma,
            net_edge_pct,
            long_fresh_pct,
            short_fresh_pct,
        )

        if net_edge_pct < self.settings.mr_min_net_edge_pct:
            return

        if symbol in self.open_positions_by_symbol or symbol in self.pending_entries_by_symbol:
            return
        if (len(self.open_positions_by_symbol) + len(self.pending_entries_by_symbol)) >= self.settings.mr_max_positions:
            return

        last_closed_at = self.last_closed_by_symbol.get(symbol)
        if last_closed_at is not None:
            cooldown = timedelta(seconds=self.settings.mr_cooldown_sec)
            if now - last_closed_at < cooldown:
                return

        age_long_ms = (now - long_quote.received_at).total_seconds() * 1000.0
        age_short_ms = (now - short_quote.received_at).total_seconds() * 1000.0
        if age_long_ms > self.settings.max_quote_age_ms or age_short_ms > self.settings.max_quote_age_ms:
            return

        long_capacity = float(long_quote.best_ask_price * long_quote.best_ask_size)
        short_capacity = float(short_quote.best_bid_price * short_quote.best_bid_size)
        if long_capacity < self.settings.mr_notional_usdt or short_capacity < self.settings.mr_notional_usdt:
            return

        planned_at = now + timedelta(milliseconds=self.settings.simulated_execution_delay_ms)
        task = asyncio.create_task(self._execute_after_delay(symbol), name=f"mr-entry-{symbol}")
        self.pending_entries_by_symbol[symbol] = PendingMrEntry(
            symbol=symbol,
            long_exchange=long_exchange,
            short_exchange=short_exchange,
            direction=direction,
            signal_spread_pct=spread_pct,
            rolling_mean=mean,
            rolling_std=std,
            sigma_at_signal=sigma,
            expected_edge_pct=expected_edge_pct,
            net_edge_pct=net_edge_pct,
            planned_at=planned_at,
            task=task,
        )

    async def _execute_after_delay(self, symbol: str) -> None:
        pending = self.pending_entries_by_symbol.get(symbol)
        if pending is None:
            return

        delay_sec = self.settings.simulated_execution_delay_ms / 1000.0
        try:
            await asyncio.sleep(delay_sec)
            current = self.pending_entries_by_symbol.get(symbol)
            if current is None:
                return

            now = datetime.now(UTC)
            if symbol in self.open_positions_by_symbol:
                return
            if len(self.open_positions_by_symbol) >= self.settings.mr_max_positions:
                return
            last_closed_at = self.last_closed_by_symbol.get(symbol)
            if last_closed_at is not None:
                cooldown = timedelta(seconds=self.settings.mr_cooldown_sec)
                if now - last_closed_at < cooldown:
                    return

            long_quote = self.get_latest_quote(current.long_exchange, symbol)
            short_quote = self.get_latest_quote(current.short_exchange, symbol)
            if long_quote is None or short_quote is None:
                return

            age_long_ms = (now - long_quote.received_at).total_seconds() * 1000.0
            age_short_ms = (now - short_quote.received_at).total_seconds() * 1000.0
            if age_long_ms > self.settings.max_quote_age_ms or age_short_ms > self.settings.max_quote_age_ms:
                return

            long_capacity = float(long_quote.best_ask_price * long_quote.best_ask_size)
            short_capacity = float(short_quote.best_bid_price * short_quote.best_bid_size)
            if long_capacity < self.settings.mr_notional_usdt or short_capacity < self.settings.mr_notional_usdt:
                return

            current_spread_pct = _directional_spread_pct(
                ask_long=long_quote.best_ask_price,
                bid_short=short_quote.best_bid_price,
            )
            threshold = current.rolling_mean + (self.settings.mr_sigma_entry * current.rolling_std)
            if current_spread_pct <= threshold:
                return
            roundtrip_cost_pct = _roundtrip_cost_pct(
                fee_long_pct=self.exchange_fees_pct.get(current.long_exchange, 0.0),
                fee_short_pct=self.exchange_fees_pct.get(current.short_exchange, 0.0),
                slippage_buffer_pct=self.settings.slippage_buffer_pct,
                safety_buffer_pct=self.settings.safety_buffer_pct,
            )
            expected_edge_pct = current_spread_pct - current.rolling_mean
            net_edge_pct = expected_edge_pct - roundtrip_cost_pct
            if net_edge_pct < self.settings.mr_min_net_edge_pct:
                return

            entry_fees_usdt = _one_side_fees_usdt(
                notional_usdt=self.settings.mr_notional_usdt,
                fee_long_pct=self.exchange_fees_pct.get(current.long_exchange, 0.0),
                fee_short_pct=self.exchange_fees_pct.get(current.short_exchange, 0.0),
            )
            entry_slippage_usdt = _one_side_slippage_usdt(
                notional_usdt=self.settings.mr_notional_usdt,
                slippage_buffer_pct=self.settings.slippage_buffer_pct,
            )

            sigma_at_entry = _sigma_from(
                mean=current.rolling_mean,
                std=current.rolling_std,
                spread_pct=current_spread_pct,
            )
            edge_at_entry = current_spread_pct - current.rolling_mean
            take_profit_target = current_spread_pct - (edge_at_entry * self.settings.mr_take_profit_fraction)
            position = MeanRevPosition(
                symbol=symbol,
                long_exchange=current.long_exchange,
                short_exchange=current.short_exchange,
                direction=current.direction,
                notional_usdt=self.settings.mr_notional_usdt,
                opened_at=now,
                entry_long_price=float(long_quote.best_ask_price),
                entry_short_price=float(short_quote.best_bid_price),
                entry_spread_pct=current_spread_pct,
                entry_rolling_mean=current.rolling_mean,
                entry_rolling_std=current.rolling_std,
                sigma_at_entry=sigma_at_entry,
                take_profit_target=take_profit_target,
                max_adverse_spread_pct=current_spread_pct,
                max_favorable_spread_pct=current_spread_pct,
                estimated_entry_fees_usdt=entry_fees_usdt,
                estimated_entry_slippage_usdt=entry_slippage_usdt,
            )
            self.open_positions_by_symbol[symbol] = position

            self.log.info(
                "mr open | %s | long=%s @ %.6f | short=%s @ %.6f | spread=%+.4f%% | mean=%+.4f%% | sigma=%.2f | target=%+.4f%%",
                symbol,
                position.long_exchange.value,
                position.entry_long_price,
                position.short_exchange.value,
                position.entry_short_price,
                position.entry_spread_pct,
                position.entry_rolling_mean,
                position.sigma_at_entry,
                position.take_profit_target,
            )
        except asyncio.CancelledError:
            raise
        finally:
            self.pending_entries_by_symbol.pop(symbol, None)

    def _close_position(
        self,
        *,
        position: MeanRevPosition,
        close_reason: str,
        long_quote: Quote | None,
        short_quote: Quote | None,
    ) -> None:
        now = datetime.now(UTC)

        if long_quote is None or short_quote is None:
            exit_long_price = position.entry_long_price
            exit_short_price = position.entry_short_price
            exit_spread_pct = position.entry_spread_pct
        else:
            exit_long_price = float(long_quote.best_bid_price)
            exit_short_price = float(short_quote.best_ask_price)
            exit_spread_pct = _directional_spread_pct(
                ask_long=long_quote.best_ask_price,
                bid_short=short_quote.best_bid_price,
            )

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
                entry_raw_spread_pct=position.entry_spread_pct,
                exit_raw_spread_pct=exit_spread_pct,
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
        self.total_hold_seconds += hold_seconds
        self.total_net_pnl_usdt += pnl.net_pnl_usdt
        self.close_reason_counts[close_reason] += 1
        self.symbol_pnl_usdt[position.symbol] += pnl.net_pnl_usdt
        if close_reason == "mean_reversion" and pnl.net_pnl_usdt > 0:
            self.winning_trades_count += 1

        self.last_closed_by_symbol[position.symbol] = now
        self.open_positions_by_symbol.pop(position.symbol, None)

        self.log.info(
            "mr close | %s | reason=%s | hold=%.0fs | net_pnl=%+.2f USDT (%+.4f%%) | entry_spread=%+.2f%% | exit_spread=%+.2f%%",
            position.symbol,
            close_reason,
            hold_seconds,
            pnl.net_pnl_usdt,
            pnl.net_pnl_pct,
            position.entry_spread_pct,
            exit_spread_pct,
        )


def _directional_spread_pct(*, ask_long: Decimal, bid_short: Decimal) -> float:
    return float((bid_short - ask_long) / ask_long * Decimal("100"))


def _roundtrip_cost_pct(
    *,
    fee_long_pct: float,
    fee_short_pct: float,
    slippage_buffer_pct: float,
    safety_buffer_pct: float,
) -> float:
    return (2.0 * (fee_long_pct + fee_short_pct)) + slippage_buffer_pct + safety_buffer_pct


def _one_side_fees_usdt(*, notional_usdt: float, fee_long_pct: float, fee_short_pct: float) -> float:
    return notional_usdt * (fee_long_pct + fee_short_pct) / 100.0


def _one_side_slippage_usdt(*, notional_usdt: float, slippage_buffer_pct: float) -> float:
    roundtrip_slippage = (2.0 * notional_usdt) * (slippage_buffer_pct / 100.0)
    return roundtrip_slippage / 2.0


def _sigma_from(*, mean: float, std: float, spread_pct: float) -> float:
    if std <= 1e-12:
        return float("inf") if spread_pct > mean else 0.0
    return (spread_pct - mean) / std


def _direction_tag(*, long_exchange: ExchangeName, short_exchange: ExchangeName) -> str:
    return "ab" if long_exchange.value < short_exchange.value else "ba"
