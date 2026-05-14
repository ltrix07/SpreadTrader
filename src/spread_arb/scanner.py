from __future__ import annotations

import asyncio
import logging
import signal
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from itertools import combinations

import aiohttp

from .config import Settings
from .exchanges import (
    BinanceExchange,
    BitgetExchange,
    BybitExchange,
    ExchangeClient,
    GateExchange,
    HtxExchange,
    MexcExchange,
    OkxExchange,
)
from .models import ExchangeName, Quote
from .mean_reversion_engine import MeanReversionEngine
from .opportunity import SpreadOpportunity, classify_opportunity
from .storage import OpportunityRecord, OpportunityStore, SpreadSnapshotRecord
from .ws_feeds import (
    BinanceWsFeed,
    BitgetWsFeed,
    BybitWsFeed,
    GateWsFeed,
    HtxWsFeed,
    OkxWsFeed,
    WebSocketFeed,
)


class QuoteScanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log = logging.getLogger(__name__)
        self.stop_event = asyncio.Event()
        self.last_quote_at: dict[tuple[ExchangeName, str], datetime] = {}
        self.latest_quotes: dict[tuple[ExchangeName, str], Quote] = {}
        self.latest_quotes_by_symbol: dict[str, dict[ExchangeName, Quote]] = {}
        self.latest_best_opportunity_by_symbol: dict[str, SpreadOpportunity] = {}
        self.latest_raw_spread_by_symbol: dict[str, SpreadOpportunity] = {}
        self._dirty_symbols: set[str] = set()
        self.rejection_counts: Counter[str] = Counter()
        self.total_scans: int = 0
        self.opportunity_store: OpportunityStore | None = None
        self.mean_reversion_engine: MeanReversionEngine | None = None

        self.exchange_fees_pct: dict[ExchangeName, float] = {
            ExchangeName.MEXC: self.settings.taker_fee_mexc_pct,
            ExchangeName.BYBIT: self.settings.taker_fee_bybit_pct,
            ExchangeName.OKX: self.settings.taker_fee_okx_pct,
            ExchangeName.BINANCE: self.settings.taker_fee_binance_pct,
            ExchangeName.GATE: self.settings.taker_fee_gate_pct,
            ExchangeName.BITGET: self.settings.taker_fee_bitget_pct,
            ExchangeName.HTX: self.settings.taker_fee_htx_pct,
        }

    async def run(self) -> None:
        self._install_signal_handlers()

        timeout = aiohttp.ClientTimeout(total=self.settings.request_timeout_sec)
        with OpportunityStore(self.settings.database_url) as self.opportunity_store:
            self.mean_reversion_engine = None
            if self.settings.mr_enabled:
                self.mean_reversion_engine = MeanReversionEngine(
                    settings=self.settings,
                    opportunity_store=self.opportunity_store,
                    get_latest_quote=self._get_latest_quote,
                )
                self.mean_reversion_engine.preload_baselines(self.settings.database_url)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if self.settings.use_websocket:
                    data_tasks = self._start_ws_feeds(session)
                    mode = "websocket"
                else:
                    data_tasks = self._start_rest_polls(session)
                    mode = "REST"

                if not data_tasks:
                    raise RuntimeError("No exchanges configured")

                self.log.info(
                    "starting quote scanner [%s] | exchanges=%s symbols=%d",
                    mode,
                    [e.value for e in self.settings.exchanges],
                    len(self.settings.symbols),
                )

                stale_task = asyncio.create_task(self._monitor_stale_quotes(), name="stale-monitor")
                spread_task = asyncio.create_task(self._log_top_spreads(), name="spread-monitor")
                quote_health_task = asyncio.create_task(self._log_quote_health(), name="quote-health")
                mr_summary_task: asyncio.Task | None = None
                if self.mean_reversion_engine is not None:
                    mr_summary_task = asyncio.create_task(
                        self.mean_reversion_engine.summary_loop(self.stop_event),
                        name="mr-summary",
                    )
                symbol_scan_task = asyncio.create_task(
                    self._scan_dirty_symbols_loop(),
                    name="symbol-scan",
                )
                snapshot_task = asyncio.create_task(
                    self._collect_spread_snapshots(),
                    name="spread-snapshots",
                )

                await self.stop_event.wait()

                bg_tasks = [
                    *data_tasks,
                    stale_task,
                    spread_task,
                    quote_health_task,
                    symbol_scan_task,
                    snapshot_task,
                ]
                if mr_summary_task is not None:
                    bg_tasks.append(mr_summary_task)
                for task in bg_tasks:
                    task.cancel()

                await asyncio.gather(*bg_tasks, return_exceptions=True)
                if self.mean_reversion_engine is not None:
                    await self.mean_reversion_engine.shutdown()
                self.log.info("scanner stopped")
        self.opportunity_store = None
        self.mean_reversion_engine = None

    def stop(self) -> None:
        self.stop_event.set()

    def _on_quote(self, quote: Quote) -> None:
        key = (quote.exchange, quote.symbol)
        self.last_quote_at[key] = quote.received_at
        self.latest_quotes[key] = quote
        self.latest_quotes_by_symbol.setdefault(quote.symbol, {})[quote.exchange] = quote
        self._dirty_symbols.add(quote.symbol)
        if self.mean_reversion_engine is not None:
            self.mean_reversion_engine.check_exits(self.latest_quotes)

    async def _scan_dirty_symbols_loop(self) -> None:
        interval_sec = max(self.settings.spread_scan_interval_sec, 0.05)

        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval_sec)
                return
            except TimeoutError:
                pass

            dirty_symbols = tuple(self._dirty_symbols)
            self._dirty_symbols.clear()
            if not dirty_symbols:
                continue

            for index, symbol in enumerate(dirty_symbols, start=1):
                self._scan_symbol(symbol)
                # Keep this worker cooperative under large symbol universes.
                if index % 5 == 0:
                    await asyncio.sleep(0)

    def _scan_symbol(self, symbol: str) -> None:
        symbol_quotes = self.latest_quotes_by_symbol.get(symbol)
        if symbol_quotes is None or len(symbol_quotes) < 2:
            return

        self.total_scans += 1
        all_spreads: list[SpreadOpportunity] = []

        for first_exchange, second_exchange in combinations(tuple(symbol_quotes.keys()), 2):
            first_quote = symbol_quotes[first_exchange]
            second_quote = symbol_quotes[second_exchange]

            forward = self._evaluate_direction(long_quote=first_quote, short_quote=second_quote)
            if forward is not None:
                all_spreads.append(forward)
                if forward.reason:
                    self.rejection_counts[forward.reason] += 1
                # Only record to DB if passed raw spread filter (avoid flooding).
                if forward.raw_spread_pct >= self.settings.min_raw_spread_pct:
                    self._record_opportunity(forward)

            reverse = self._evaluate_direction(long_quote=second_quote, short_quote=first_quote)
            if reverse is not None:
                all_spreads.append(reverse)
                if reverse.reason:
                    self.rejection_counts[reverse.reason] += 1
                if reverse.raw_spread_pct >= self.settings.min_raw_spread_pct:
                    self._record_opportunity(reverse)

        # Diagnostic: always track best raw spread per symbol (regardless of filters).
        if all_spreads:
            best_raw = max(all_spreads, key=lambda item: item.raw_spread_pct)
            self.latest_raw_spread_by_symbol[symbol] = best_raw

        # Track observed opportunities for diagnostics/logging.
        observed = [s for s in all_spreads if s.status == "observed"]
        if observed:
            self.latest_best_opportunity_by_symbol[symbol] = max(
                observed,
                key=lambda item: item.estimated_net_spread_pct,
            )

    def _evaluate_direction(self, *, long_quote: Quote, short_quote: Quote) -> SpreadOpportunity | None:
        ask_long = long_quote.best_ask_price
        bid_short = short_quote.best_bid_price

        if ask_long <= Decimal("0"):
            return None

        raw_spread_pct = float((bid_short - ask_long) / ask_long * Decimal("100"))

        now = datetime.now(UTC)
        quote_age_ms_long = (now - long_quote.received_at).total_seconds() * 1000.0
        quote_age_ms_short = (now - short_quote.received_at).total_seconds() * 1000.0

        fee_long = self.exchange_fees_pct.get(long_quote.exchange, 0.0)
        fee_short = self.exchange_fees_pct.get(short_quote.exchange, 0.0)
        estimated_roundtrip_cost_pct = (2.0 * fee_long) + (2.0 * fee_short)
        estimated_roundtrip_cost_pct += self.settings.slippage_buffer_pct + self.settings.safety_buffer_pct
        estimated_net_spread_pct = raw_spread_pct - estimated_roundtrip_cost_pct

        max_age_ms = self.settings.max_quote_age_ms
        is_fresh = quote_age_ms_long <= max_age_ms and quote_age_ms_short <= max_age_ms

        long_notional_capacity = float(long_quote.best_ask_price * long_quote.best_ask_size)
        short_notional_capacity = float(short_quote.best_bid_price * short_quote.best_bid_size)
        is_liquid = (
            long_notional_capacity >= self.settings.paper_notional_usdt
            and short_notional_capacity >= self.settings.paper_notional_usdt
        )

        # Classify: raw spread threshold checked first, then fine-grained filters.
        if raw_spread_pct < self.settings.min_raw_spread_pct:
            status = "rejected"
            reason = "raw_spread_below_min"
        else:
            decision = classify_opportunity(
                estimated_net_spread_pct=estimated_net_spread_pct,
                entry_net_spread_pct=self.settings.entry_net_spread_pct,
                is_fresh=is_fresh,
                is_liquid=is_liquid,
            )
            status = decision.status
            reason = decision.reason

        return SpreadOpportunity(
            symbol=long_quote.symbol,
            long_exchange=long_quote.exchange,
            short_exchange=short_quote.exchange,
            ask_long=float(long_quote.best_ask_price),
            bid_short=float(short_quote.best_bid_price),
            raw_spread_pct=raw_spread_pct,
            estimated_net_spread_pct=estimated_net_spread_pct,
            estimated_roundtrip_cost_pct=estimated_roundtrip_cost_pct,
            available_long_size=float(long_quote.best_ask_size),
            available_short_size=float(short_quote.best_bid_size),
            quote_age_ms_long=quote_age_ms_long,
            quote_age_ms_short=quote_age_ms_short,
            notional_usdt=self.settings.paper_notional_usdt,
            status=status,
            reason=reason,
            timestamp=now,
        )

    def _record_opportunity(self, opportunity: SpreadOpportunity) -> None:
        if self.opportunity_store is None:
            return

        record = OpportunityRecord(
            timestamp=opportunity.timestamp.isoformat(),
            symbol=opportunity.symbol,
            long_exchange=opportunity.long_exchange.value,
            short_exchange=opportunity.short_exchange.value,
            ask_long=opportunity.ask_long,
            bid_short=opportunity.bid_short,
            raw_spread_pct=opportunity.raw_spread_pct,
            estimated_net_spread_pct=opportunity.estimated_net_spread_pct,
            estimated_roundtrip_cost_pct=opportunity.estimated_roundtrip_cost_pct,
            available_long_size=opportunity.available_long_size,
            available_short_size=opportunity.available_short_size,
            quote_age_ms_long=opportunity.quote_age_ms_long,
            quote_age_ms_short=opportunity.quote_age_ms_short,
            notional_usdt=opportunity.notional_usdt,
            status=opportunity.status,
            reason=opportunity.reason,
            created_at=OpportunityStore.now_iso(),
        )
        opportunity.opportunity_id = self.opportunity_store.insert_opportunity(record)

        if opportunity.status == "observed":
            self.log.info(
                "observed | %s | long=%s ask=%.6f | short=%s bid=%.6f | raw=%.4f%% | net=%.4f%% | cost=%.4f%%",
                opportunity.symbol,
                opportunity.long_exchange.value,
                opportunity.ask_long,
                opportunity.short_exchange.value,
                opportunity.bid_short,
                opportunity.raw_spread_pct,
                opportunity.estimated_net_spread_pct,
                opportunity.estimated_roundtrip_cost_pct,
            )
        else:
            self.log.debug(
                "rejected | %s | long=%s short=%s | raw=%.4f%% | net=%.4f%% | reason=%s",
                opportunity.symbol,
                opportunity.long_exchange.value,
                opportunity.short_exchange.value,
                opportunity.raw_spread_pct,
                opportunity.estimated_net_spread_pct,
                opportunity.reason,
            )

    def _get_latest_quote(self, exchange: ExchangeName, symbol: str) -> Quote | None:
        return self.latest_quotes.get((exchange, symbol))

    async def _monitor_stale_quotes(self) -> None:
        interval_sec = max(self.settings.max_quote_age_ms / 1000.0 / 2.0, 0.5)

        while not self.stop_event.is_set():
            now = datetime.now(UTC)
            max_age_ms = self.settings.max_quote_age_ms
            for key, ts in self.last_quote_at.items():
                age_ms = (now - ts).total_seconds() * 1000.0
                if age_ms > max_age_ms:
                    exchange, symbol = key
                    self.log.warning(
                        "stale quote detected | %s | %s | age_ms=%.0f > max=%d",
                        exchange.value,
                        symbol,
                        age_ms,
                        max_age_ms,
                    )

            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval_sec)
            except TimeoutError:
                continue

    async def _log_top_spreads(self) -> None:
        interval_sec = self.settings.top_spreads_log_interval_sec

        while not self.stop_event.is_set():
            # Show ALL raw spreads (including sub-threshold) sorted by raw spread.
            if self.latest_raw_spread_by_symbol:
                top = sorted(
                    self.latest_raw_spread_by_symbol.values(),
                    key=lambda item: item.raw_spread_pct,
                    reverse=True,
                )[:10]
                lines = [
                    f"  {item.symbol:<16s} "
                    f"{item.long_exchange.value}->{item.short_exchange.value}  "
                    f"raw={item.raw_spread_pct:+.4f}%  "
                    f"net={item.estimated_net_spread_pct:+.4f}%  "
                    f"cost={item.estimated_roundtrip_cost_pct:.4f}%  "
                    f"age_l={item.quote_age_ms_long:.0f}ms  "
                    f"age_s={item.quote_age_ms_short:.0f}ms  "
                    f"[{item.status}{f':{item.reason}' if item.reason else ''}]"
                    for item in top
                ]
                self.log.info("top raw spreads (all, best direction per symbol):\n%s", "\n".join(lines))

            # Rejection breakdown.
            if self.rejection_counts:
                breakdown = " | ".join(
                    f"{reason}={count}" for reason, count in self.rejection_counts.most_common()
                )
                self.log.info(
                    "rejections (cumulative) | %s | total_scans=%d",
                    breakdown,
                    self.total_scans,
                )

            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval_sec)
            except TimeoutError:
                continue

    async def _log_quote_health(self) -> None:
        interval_sec = 10.0

        while not self.stop_event.is_set():
            if self.latest_quotes:
                now = datetime.now(UTC)
                lines: list[str] = []
                for (exchange, symbol), quote in sorted(
                    self.latest_quotes.items(),
                    key=lambda x: (x[0][1], x[0][0].value),
                ):
                    age_ms = (now - quote.received_at).total_seconds() * 1000.0
                    stale_marker = " STALE" if age_ms > self.settings.max_quote_age_ms else ""
                    inner_spread_bps = (
                        float(
                            (quote.best_ask_price - quote.best_bid_price)
                            / quote.best_ask_price
                            * Decimal("10000")
                        )
                        if quote.best_ask_price > 0
                        else 0.0
                    )
                    lines.append(
                        f"  {symbol:<16s} {exchange.value:<6s}  "
                        f"bid={str(quote.best_bid_price):<14s}  "
                        f"ask={str(quote.best_ask_price):<14s}  "
                        f"bbo_bps={inner_spread_bps:6.1f}  "
                        f"age={age_ms:7.0f}ms  "
                        f"lat={quote.receive_latency_ms:5.0f}ms"
                        f"{stale_marker}"
                    )
                self.log.info("quote health:\n%s", "\n".join(lines))

            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval_sec)
            except TimeoutError:
                continue

    async def _collect_spread_snapshots(self) -> None:
        """Periodically snapshot spreads for all symbol x exchange-pair combos.

        Runs every 10 seconds, records current spread state for mean-reversion
        analysis.  Only records pairs where both quotes are reasonably fresh
        (< 30 s) to avoid polluting the dataset with stale data.
        """
        interval_sec = 10.0
        max_snapshot_age_ms = 30_000.0  # generous: we want the full picture
        snapshot_count = 0
        cycle_count = 0
        error_count = 0

        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval_sec)
                return
            except TimeoutError:
                pass

            if self.opportunity_store is None:
                continue

            cycle_count += 1

            try:
                now = datetime.now(UTC)
                ts_iso = now.isoformat()
                records: list[SpreadSnapshotRecord] = []

                # Snapshot current quotes to avoid dict-changed-during-iteration.
                snapshot_quotes = dict(self.latest_quotes)
                if self.mean_reversion_engine is not None:
                    self.mean_reversion_engine.update_baselines(snapshot_quotes)

                # Group quotes by symbol.
                quotes_by_symbol: dict[str, dict[ExchangeName, Quote]] = {}
                for (exchange, symbol), quote in snapshot_quotes.items():
                    quotes_by_symbol.setdefault(symbol, {})[exchange] = quote

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

                            if age_a_ms > max_snapshot_age_ms or age_b_ms > max_snapshot_age_ms:
                                continue

                            ask_a = float(q_a.best_ask_price)
                            bid_a = float(q_a.best_bid_price)
                            ask_b = float(q_b.best_ask_price)
                            bid_b = float(q_b.best_bid_price)

                            # Direction A->B: long A (buy ask_a), short B (sell bid_b).
                            spread_ab = (bid_b - ask_a) / ask_a * 100.0 if ask_a > 0 else 0.0
                            # Direction B->A: long B (buy ask_b), short A (sell bid_a).
                            spread_ba = (bid_a - ask_b) / ask_b * 100.0 if ask_b > 0 else 0.0

                            # Ensure consistent ordering: exchange_a < exchange_b alphabetically.
                            if ex_a.value > ex_b.value:
                                ex_a, ex_b = ex_b, ex_a
                                spread_ab, spread_ba = spread_ba, spread_ab
                                bid_a, bid_b = bid_b, bid_a
                                ask_a, ask_b = ask_b, ask_a
                                age_a_ms, age_b_ms = age_b_ms, age_a_ms

                            records.append(SpreadSnapshotRecord(
                                timestamp=ts_iso,
                                symbol=symbol,
                                exchange_a=ex_a.value,
                                exchange_b=ex_b.value,
                                bid_a=bid_a,
                                ask_a=ask_a,
                                bid_b=bid_b,
                                ask_b=ask_b,
                                raw_spread_ab_pct=spread_ab,
                                raw_spread_ba_pct=spread_ba,
                                best_raw_spread_pct=max(spread_ab, spread_ba),
                                quote_age_a_ms=age_a_ms,
                                quote_age_b_ms=age_b_ms,
                            ))

                if records:
                    inserted = self.opportunity_store.insert_spread_snapshots(records)
                    snapshot_count += inserted

                # Log progress every ~60 seconds (every 6th cycle).
                if cycle_count % 6 == 0:
                    self.log.info(
                        "spread snapshots | cycle=%d batch=%d total=%d errors=%d quotes=%d",
                        cycle_count,
                        len(records),
                        snapshot_count,
                        error_count,
                        len(snapshot_quotes),
                    )

            except Exception as exc:  # noqa: BLE001
                error_count += 1
                self.log.warning(
                    "spread snapshot error (cycle=%d, errors=%d): %s",
                    cycle_count, error_count, exc,
                    exc_info=True,
                )

    def _start_ws_feeds(self, session: aiohttp.ClientSession) -> list[asyncio.Task]:
        """Create WebSocket feed tasks for all configured exchanges."""
        ws_factory: dict[ExchangeName, type[WebSocketFeed]] = {
            ExchangeName.OKX: OkxWsFeed,
            ExchangeName.BYBIT: BybitWsFeed,
            ExchangeName.BINANCE: BinanceWsFeed,
            ExchangeName.GATE: GateWsFeed,
            ExchangeName.BITGET: BitgetWsFeed,
            ExchangeName.HTX: HtxWsFeed,
        }

        tasks: list[asyncio.Task] = []
        for exchange_name in self.settings.exchanges:
            feed_cls = ws_factory.get(exchange_name)
            if feed_cls is None:
                self.log.warning("no WS feed for exchange %s, skipping", exchange_name)
                continue
            feed = feed_cls(
                session=session,
                symbols=self.settings.symbols,
                on_quote=self._on_quote,
            )
            tasks.append(
                asyncio.create_task(
                    feed.run(self.stop_event),
                    name=f"ws-{exchange_name.value}",
                )
            )
        return tasks

    def _start_rest_polls(self, session: aiohttp.ClientSession) -> list[asyncio.Task]:
        """Create REST polling tasks for all configured exchanges (legacy mode)."""
        exchanges = self._build_exchanges(session)
        return [
            asyncio.create_task(
                exchange.poll(
                    symbols=self.settings.symbols,
                    on_quote=self._on_quote,
                    stop_event=self.stop_event,
                    poll_interval_sec=self.settings.poll_interval_sec,
                    reconnect_backoff_sec=self.settings.reconnect_backoff_sec,
                ),
                name=f"poll-{exchange.name.value}",
            )
            for exchange in exchanges
        ]

    def _build_exchanges(self, session: aiohttp.ClientSession) -> list[ExchangeClient]:
        by_name: dict[ExchangeName, type[ExchangeClient]] = {
            ExchangeName.MEXC: MexcExchange,
            ExchangeName.BYBIT: BybitExchange,
            ExchangeName.OKX: OkxExchange,
            ExchangeName.BINANCE: BinanceExchange,
            ExchangeName.GATE: GateExchange,
            ExchangeName.BITGET: BitgetExchange,
            ExchangeName.HTX: HtxExchange,
        }

        result: list[ExchangeClient] = []
        for exchange_name in self.settings.exchanges:
            factory = by_name.get(exchange_name)
            if factory is None:
                self.log.warning("unknown exchange in config, skipping: %s", exchange_name)
                continue
            result.append(factory(session=session, request_timeout_sec=self.settings.request_timeout_sec))
        return result

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def _request_shutdown(sig_name: str) -> None:
            self.log.info("received %s, shutting down", sig_name)
            self.stop()

        for sig in _supported_signals():
            try:
                loop.add_signal_handler(sig, lambda s=sig: _request_shutdown(s.name))
            except NotImplementedError:
                # On some Windows event loops add_signal_handler is unavailable.
                pass


def _supported_signals() -> Iterable[signal.Signals]:
    available = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        available.append(signal.SIGTERM)
    return available
