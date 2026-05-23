#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import signal
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if TYPE_CHECKING:
    import aiohttp

log = logging.getLogger("spread_lifecycle")

FRESH_QUOTE_MAX_AGE_SEC = 5.0
TICK_SAMPLE_INTERVAL_MS = 200
STATS_INTERVAL_SEC = 60.0
COOLDOWN_SWEEP_INTERVAL_SEC = 0.2

TRACKED_EXCHANGES: tuple[str, ...] = (
    "binance",
    "bybit",
    "bitget",
    "gate",
    "okx",
)


def iso_utc(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def compute_directional_spread_pct(ask_long: float, bid_short: float) -> float:
    if ask_long <= 0:
        return 0.0
    return (bid_short - ask_long) / ask_long * 100.0


def compute_quote_bbo_bps(quote: Any) -> float | None:
    bid = float(quote.best_bid_price)
    ask = float(quote.best_ask_price)
    if bid <= 0:
        return None
    return (ask - bid) / bid * 10_000.0


@dataclass(slots=True)
class SpreadSample:
    ts: datetime
    spread_pct: float
    bid_short: float
    ask_long: float
    bbo_long_bps: float | None
    bbo_short_bps: float | None


@dataclass(slots=True)
class TickSample:
    ts: datetime
    spread_pct: float
    bid_short: float
    ask_long: float
    bbo_long_bps: float | None
    bbo_short_bps: float | None


@dataclass(slots=True)
class EventRecord:
    symbol: str
    long_exchange: str
    short_exchange: str
    started_at: datetime
    peaked_at: datetime
    ended_at: datetime
    duration_ms: int
    rise_duration_ms: int
    fall_duration_ms: int
    threshold_pct: float
    entry_spread_pct: float
    peak_spread_pct: float
    exit_spread_pct: float
    mean_spread_pct: float
    rise_velocity: float
    fall_velocity: float
    num_samples: int
    time_above_peak50_ms: int
    time_above_peak75_ms: int
    spread_std: float
    avg_bbo_long_bps: float | None
    avg_bbo_short_bps: float | None
    simulated_gross_pnl_pct: float
    simulated_net_pnl_pct: float
    ticks: list[TickSample]


@dataclass(slots=True)
class ActiveEvent:
    symbol: str
    long_exchange: str
    short_exchange: str
    started_at: datetime
    threshold_pct: float
    samples: list[SpreadSample] = field(default_factory=list)
    bbo_long_samples: list[float] = field(default_factory=list)
    bbo_short_samples: list[float] = field(default_factory=list)
    peak_spread_pct: float = 0.0
    peaked_at: datetime | None = None
    last_above_threshold_at: datetime | None = None
    last_tick_recorded_at: datetime | None = None
    last_spread_pct: float = 0.0
    ticks: list[TickSample] = field(default_factory=list)
    pending_close_started_at: datetime | None = None
    pending_close_sample_index: int | None = None
    pending_close_spread_pct: float | None = None
    pending_close_bid_short: float | None = None
    pending_close_ask_long: float | None = None
    pending_close_bbo_long_bps: float | None = None
    pending_close_bbo_short_bps: float | None = None


class LifecycleDatabase:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS spread_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                long_exchange TEXT NOT NULL,
                short_exchange TEXT NOT NULL,
                started_at TEXT NOT NULL,
                peaked_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                rise_duration_ms INTEGER NOT NULL,
                fall_duration_ms INTEGER NOT NULL,
                threshold_pct REAL NOT NULL,
                entry_spread_pct REAL NOT NULL,
                peak_spread_pct REAL NOT NULL,
                exit_spread_pct REAL NOT NULL,
                mean_spread_pct REAL NOT NULL,
                rise_velocity REAL NOT NULL,
                fall_velocity REAL NOT NULL,
                num_samples INTEGER NOT NULL,
                time_above_peak50_ms INTEGER NOT NULL,
                time_above_peak75_ms INTEGER NOT NULL,
                spread_std REAL NOT NULL,
                avg_bbo_long_bps REAL,
                avg_bbo_short_bps REAL,
                simulated_gross_pnl_pct REAL NOT NULL,
                simulated_net_pnl_pct REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_spread_events_symbol ON spread_events(symbol);
            CREATE INDEX IF NOT EXISTS idx_spread_events_pair ON spread_events(long_exchange, short_exchange);
            CREATE INDEX IF NOT EXISTS idx_spread_events_peak ON spread_events(peak_spread_pct);
            CREATE INDEX IF NOT EXISTS idx_spread_events_started ON spread_events(started_at);

            CREATE TABLE IF NOT EXISTS spread_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES spread_events(id),
                ts TEXT NOT NULL,
                spread_pct REAL NOT NULL,
                bid_short REAL NOT NULL,
                ask_long REAL NOT NULL,
                bbo_long_bps REAL,
                bbo_short_bps REAL,
                elapsed_ms INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_spread_ticks_event ON spread_ticks(event_id);
            """
        )
        self.conn.commit()

    def insert_event(self, event: EventRecord) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO spread_events (
                symbol, long_exchange, short_exchange,
                started_at, peaked_at, ended_at,
                duration_ms, rise_duration_ms, fall_duration_ms,
                threshold_pct, entry_spread_pct, peak_spread_pct, exit_spread_pct,
                mean_spread_pct, rise_velocity, fall_velocity,
                num_samples, time_above_peak50_ms, time_above_peak75_ms, spread_std,
                avg_bbo_long_bps, avg_bbo_short_bps,
                simulated_gross_pnl_pct, simulated_net_pnl_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.symbol,
                event.long_exchange,
                event.short_exchange,
                iso_utc(event.started_at),
                iso_utc(event.peaked_at),
                iso_utc(event.ended_at),
                event.duration_ms,
                event.rise_duration_ms,
                event.fall_duration_ms,
                event.threshold_pct,
                event.entry_spread_pct,
                event.peak_spread_pct,
                event.exit_spread_pct,
                event.mean_spread_pct,
                event.rise_velocity,
                event.fall_velocity,
                event.num_samples,
                event.time_above_peak50_ms,
                event.time_above_peak75_ms,
                event.spread_std,
                event.avg_bbo_long_bps,
                event.avg_bbo_short_bps,
                event.simulated_gross_pnl_pct,
                event.simulated_net_pnl_pct,
            ),
        )
        event_id = int(cur.lastrowid)
        if event.ticks:
            self.conn.executemany(
                """
                INSERT INTO spread_ticks (
                    event_id, ts, spread_pct, bid_short, ask_long,
                    bbo_long_bps, bbo_short_bps, elapsed_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        event_id,
                        iso_utc(tick.ts),
                        tick.spread_pct,
                        tick.bid_short,
                        tick.ask_long,
                        tick.bbo_long_bps,
                        tick.bbo_short_bps,
                        max(int((tick.ts - event.started_at).total_seconds() * 1000), 0),
                    )
                    for tick in event.ticks
                ],
            )
        self.conn.commit()
        return event_id

    def close(self) -> None:
        self.conn.close()


class SpreadLifecycleTracker:
    def __init__(
        self,
        *,
        threshold_pct: float,
        cooldown_sec: float,
        tick_interval_ms: int,
        fee_pct_by_exchange: dict[str, float],
        slippage_buffer_pct: float,
        safety_buffer_pct: float,
        db: LifecycleDatabase,
    ) -> None:
        self.threshold_pct = threshold_pct
        self.cooldown_sec = cooldown_sec
        self.tick_interval_ms = tick_interval_ms
        self.fee_pct_by_exchange = fee_pct_by_exchange
        self.slippage_buffer_pct = slippage_buffer_pct
        self.safety_buffer_pct = safety_buffer_pct
        self.db = db
        self.active_events: dict[tuple[str, str, str], ActiveEvent] = {}
        self.total_events_recorded = 0
        self.started_run_at = datetime.now(UTC)

    def process_update(
        self,
        *,
        symbol: str,
        long_exchange: str,
        short_exchange: str,
        spread_pct: float,
        ts: datetime,
        bid_short: float,
        ask_long: float,
        bbo_long_bps: float | None,
        bbo_short_bps: float | None,
    ) -> None:
        key = (symbol, long_exchange, short_exchange)
        event = self.active_events.get(key)

        if event is None:
            if spread_pct <= self.threshold_pct:
                return
            event = ActiveEvent(
                symbol=symbol,
                long_exchange=long_exchange,
                short_exchange=short_exchange,
                started_at=ts,
                threshold_pct=self.threshold_pct,
                peak_spread_pct=spread_pct,
                peaked_at=ts,
                last_above_threshold_at=ts,
                last_spread_pct=spread_pct,
            )
            self.active_events[key] = event
            self._append_sample(
                event,
                ts=ts,
                spread_pct=spread_pct,
                bid_short=bid_short,
                ask_long=ask_long,
                bbo_long_bps=bbo_long_bps,
                bbo_short_bps=bbo_short_bps,
            )
            self._record_tick(
                event,
                ts=ts,
                spread_pct=spread_pct,
                bid_short=bid_short,
                ask_long=ask_long,
                bbo_long_bps=bbo_long_bps,
                bbo_short_bps=bbo_short_bps,
                force=True,
            )
            return

        self._append_sample(
            event,
            ts=ts,
            spread_pct=spread_pct,
            bid_short=bid_short,
            ask_long=ask_long,
            bbo_long_bps=bbo_long_bps,
            bbo_short_bps=bbo_short_bps,
        )
        event.last_spread_pct = spread_pct

        if spread_pct > event.peak_spread_pct:
            event.peak_spread_pct = spread_pct
            event.peaked_at = ts
            self._record_tick(
                event,
                ts=ts,
                spread_pct=spread_pct,
                bid_short=bid_short,
                ask_long=ask_long,
                bbo_long_bps=bbo_long_bps,
                bbo_short_bps=bbo_short_bps,
                force=True,
            )

        self._record_tick(
            event,
            ts=ts,
            spread_pct=spread_pct,
            bid_short=bid_short,
            ask_long=ask_long,
            bbo_long_bps=bbo_long_bps,
            bbo_short_bps=bbo_short_bps,
            force=False,
        )

        if spread_pct > self.threshold_pct:
            event.last_above_threshold_at = ts
            event.pending_close_started_at = None
            event.pending_close_sample_index = None
            event.pending_close_spread_pct = None
            event.pending_close_bid_short = None
            event.pending_close_ask_long = None
            event.pending_close_bbo_long_bps = None
            event.pending_close_bbo_short_bps = None
            return

        if event.pending_close_started_at is None:
            event.pending_close_started_at = ts
            event.pending_close_sample_index = len(event.samples) - 1
            event.pending_close_spread_pct = spread_pct
            event.pending_close_bid_short = bid_short
            event.pending_close_ask_long = ask_long
            event.pending_close_bbo_long_bps = bbo_long_bps
            event.pending_close_bbo_short_bps = bbo_short_bps

        self._try_close_event(key, event, now=ts)

    def close_expired_cooldowns(self, now: datetime) -> None:
        for key, event in list(self.active_events.items()):
            self._try_close_event(key, event, now=now)

    def close_all_active(self, ended_at: datetime) -> None:
        for key, event in list(self.active_events.items()):
            if not event.samples:
                self.active_events.pop(key, None)
                continue
            last = event.samples[-1]
            record = self._build_event_record(
                event=event,
                ended_at=ended_at,
                end_sample_index=len(event.samples) - 1,
                exit_spread_pct=last.spread_pct,
                exit_bid_short=last.bid_short,
                exit_ask_long=last.ask_long,
                exit_bbo_long_bps=last.bbo_long_bps,
                exit_bbo_short_bps=last.bbo_short_bps,
            )
            self.db.insert_event(record)
            self.total_events_recorded += 1
            self.active_events.pop(key, None)

    def top_active_events(self, top_n: int = 3) -> list[ActiveEvent]:
        values = list(self.active_events.values())
        values.sort(key=lambda item: item.last_spread_pct, reverse=True)
        return values[:top_n]

    def _try_close_event(self, key: tuple[str, str, str], event: ActiveEvent, now: datetime) -> None:
        if event.pending_close_started_at is None:
            return
        if (now - event.pending_close_started_at).total_seconds() < self.cooldown_sec:
            return
        end_sample_index = event.pending_close_sample_index
        exit_spread_pct = event.pending_close_spread_pct
        exit_bid_short = event.pending_close_bid_short
        exit_ask_long = event.pending_close_ask_long
        exit_bbo_long_bps = event.pending_close_bbo_long_bps
        exit_bbo_short_bps = event.pending_close_bbo_short_bps
        if (
            end_sample_index is None
            or exit_spread_pct is None
            or exit_bid_short is None
            or exit_ask_long is None
        ):
            return

        record = self._build_event_record(
            event=event,
            ended_at=event.pending_close_started_at,
            end_sample_index=end_sample_index,
            exit_spread_pct=exit_spread_pct,
            exit_bid_short=exit_bid_short,
            exit_ask_long=exit_ask_long,
            exit_bbo_long_bps=exit_bbo_long_bps,
            exit_bbo_short_bps=exit_bbo_short_bps,
        )
        self.db.insert_event(record)
        self.total_events_recorded += 1
        self.active_events.pop(key, None)

    def _build_event_record(
        self,
        *,
        event: ActiveEvent,
        ended_at: datetime,
        end_sample_index: int,
        exit_spread_pct: float,
        exit_bid_short: float,
        exit_ask_long: float,
        exit_bbo_long_bps: float | None,
        exit_bbo_short_bps: float | None,
    ) -> EventRecord:
        samples = event.samples[: end_sample_index + 1]
        if not samples:
            raise RuntimeError("cannot finalize event without samples")

        self._record_tick(
            event,
            ts=ended_at,
            spread_pct=exit_spread_pct,
            bid_short=exit_bid_short,
            ask_long=exit_ask_long,
            bbo_long_bps=exit_bbo_long_bps,
            bbo_short_bps=exit_bbo_short_bps,
            force=True,
        )
        filtered_ticks = [tick for tick in event.ticks if tick.ts <= ended_at]
        if not filtered_ticks or filtered_ticks[-1].ts != ended_at:
            filtered_ticks.append(
                TickSample(
                    ts=ended_at,
                    spread_pct=exit_spread_pct,
                    bid_short=exit_bid_short,
                    ask_long=exit_ask_long,
                    bbo_long_bps=exit_bbo_long_bps,
                    bbo_short_bps=exit_bbo_short_bps,
                )
            )

        entry_spread_pct = samples[0].spread_pct
        peak_spread_pct = event.peak_spread_pct
        peaked_at = event.peaked_at or event.started_at

        duration_ms = max(int((ended_at - event.started_at).total_seconds() * 1000), 0)
        rise_duration_ms = max(int((peaked_at - event.started_at).total_seconds() * 1000), 0)
        fall_duration_ms = max(int((ended_at - peaked_at).total_seconds() * 1000), 0)

        spreads = [sample.spread_pct for sample in samples]
        mean_spread_pct = sum(spreads) / len(spreads)
        spread_std = math.sqrt(sum((value - mean_spread_pct) ** 2 for value in spreads) / len(spreads))

        rise_velocity = (
            (peak_spread_pct - entry_spread_pct) / (rise_duration_ms / 1000.0)
            if rise_duration_ms > 0
            else 0.0
        )
        fall_velocity = (
            (peak_spread_pct - exit_spread_pct) / (fall_duration_ms / 1000.0)
            if fall_duration_ms > 0
            else 0.0
        )

        peak_amplitude = max(peak_spread_pct - event.threshold_pct, 0.0)
        level_50 = event.threshold_pct + 0.50 * peak_amplitude
        level_75 = event.threshold_pct + 0.75 * peak_amplitude
        time_above_peak50_ms = self._time_above_level_ms(samples, ended_at, level_50)
        time_above_peak75_ms = self._time_above_level_ms(samples, ended_at, level_75)

        avg_bbo_long_bps = (
            sum(event.bbo_long_samples) / len(event.bbo_long_samples)
            if event.bbo_long_samples
            else None
        )
        avg_bbo_short_bps = (
            sum(event.bbo_short_samples) / len(event.bbo_short_samples)
            if event.bbo_short_samples
            else None
        )

        fee_long_pct = self.fee_pct_by_exchange.get(event.long_exchange, 0.0)
        fee_short_pct = self.fee_pct_by_exchange.get(event.short_exchange, 0.0)
        roundtrip_cost_pct = (
            2.0 * fee_long_pct
            + 2.0 * fee_short_pct
            + self.slippage_buffer_pct
            + self.safety_buffer_pct
        )
        simulated_gross_pnl_pct = entry_spread_pct - event.threshold_pct
        simulated_net_pnl_pct = simulated_gross_pnl_pct - roundtrip_cost_pct

        return EventRecord(
            symbol=event.symbol,
            long_exchange=event.long_exchange,
            short_exchange=event.short_exchange,
            started_at=event.started_at,
            peaked_at=peaked_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            rise_duration_ms=rise_duration_ms,
            fall_duration_ms=fall_duration_ms,
            threshold_pct=event.threshold_pct,
            entry_spread_pct=entry_spread_pct,
            peak_spread_pct=peak_spread_pct,
            exit_spread_pct=exit_spread_pct,
            mean_spread_pct=mean_spread_pct,
            rise_velocity=rise_velocity,
            fall_velocity=fall_velocity,
            num_samples=len(samples),
            time_above_peak50_ms=time_above_peak50_ms,
            time_above_peak75_ms=time_above_peak75_ms,
            spread_std=spread_std,
            avg_bbo_long_bps=avg_bbo_long_bps,
            avg_bbo_short_bps=avg_bbo_short_bps,
            simulated_gross_pnl_pct=simulated_gross_pnl_pct,
            simulated_net_pnl_pct=simulated_net_pnl_pct,
            ticks=filtered_ticks,
        )

    @staticmethod
    def _time_above_level_ms(samples: list[SpreadSample], ended_at: datetime, level: float) -> int:
        if not samples:
            return 0
        total_ms = 0.0
        for idx, sample in enumerate(samples):
            if idx + 1 < len(samples):
                next_ts = samples[idx + 1].ts
            else:
                next_ts = ended_at
            if next_ts <= sample.ts:
                continue
            if sample.spread_pct >= level:
                total_ms += (next_ts - sample.ts).total_seconds() * 1000.0
        return max(int(total_ms), 0)

    @staticmethod
    def _append_sample(
        event: ActiveEvent,
        *,
        ts: datetime,
        spread_pct: float,
        bid_short: float,
        ask_long: float,
        bbo_long_bps: float | None,
        bbo_short_bps: float | None,
    ) -> None:
        event.samples.append(
            SpreadSample(
                ts=ts,
                spread_pct=spread_pct,
                bid_short=bid_short,
                ask_long=ask_long,
                bbo_long_bps=bbo_long_bps,
                bbo_short_bps=bbo_short_bps,
            )
        )
        if bbo_long_bps is not None:
            event.bbo_long_samples.append(bbo_long_bps)
        if bbo_short_bps is not None:
            event.bbo_short_samples.append(bbo_short_bps)

    def _record_tick(
        self,
        event: ActiveEvent,
        *,
        ts: datetime,
        spread_pct: float,
        bid_short: float,
        ask_long: float,
        bbo_long_bps: float | None,
        bbo_short_bps: float | None,
        force: bool,
    ) -> None:
        if not force and event.last_tick_recorded_at is not None:
            elapsed_ms = (ts - event.last_tick_recorded_at).total_seconds() * 1000.0
            if elapsed_ms < self.tick_interval_ms:
                return
        tick = TickSample(
            ts=ts,
            spread_pct=spread_pct,
            bid_short=bid_short,
            ask_long=ask_long,
            bbo_long_bps=bbo_long_bps,
            bbo_short_bps=bbo_short_bps,
        )
        if event.ticks and event.ticks[-1].ts == ts:
            event.ticks[-1] = tick
        else:
            event.ticks.append(tick)
        event.last_tick_recorded_at = ts


class SpreadLifecycleCollector:
    def __init__(
        self,
        *,
        threshold_pct: float,
        db_path: Path,
        include_dynamic: bool,
        cooldown_sec: float,
        max_hours: float,
    ) -> None:
        from spread_arb.config import get_settings

        self.settings = get_settings()
        self.threshold_pct = threshold_pct
        self.db_path = db_path
        self.include_dynamic = include_dynamic
        self.cooldown_sec = cooldown_sec
        self.max_hours = max_hours

        self.stop_event = asyncio.Event()
        self.latest_quotes_by_symbol: dict[str, dict[str, Any]] = {}

        fees = {
            "binance": self.settings.taker_fee_binance_pct,
            "bybit": self.settings.taker_fee_bybit_pct,
            "bitget": self.settings.taker_fee_bitget_pct,
            "gate": self.settings.taker_fee_gate_pct,
            "okx": self.settings.taker_fee_okx_pct,
        }
        self.db = LifecycleDatabase(self.db_path)
        self.tracker = SpreadLifecycleTracker(
            threshold_pct=threshold_pct,
            cooldown_sec=cooldown_sec,
            tick_interval_ms=TICK_SAMPLE_INTERVAL_MS,
            fee_pct_by_exchange=fees,
            slippage_buffer_pct=self.settings.slippage_buffer_pct,
            safety_buffer_pct=self.settings.safety_buffer_pct,
            db=self.db,
        )
        self._run_started_at = datetime.now(UTC)

    async def run(self) -> None:
        self._install_signal_handlers()
        import aiohttp
        from spread_arb.models import ExchangeName
        from spread_arb.ws_feeds import (
            BinanceWsFeed,
            BitgetWsFeed,
            BybitWsFeed,
            GateWsFeed,
            OkxWsFeed,
        )

        timeout = aiohttp.ClientTimeout(total=self.settings.request_timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            symbols = await self._build_symbol_universe(session)
            if not symbols:
                raise RuntimeError("No symbols to monitor.")

            ws_factory = {
                "binance": BinanceWsFeed,
                "bybit": BybitWsFeed,
                "bitget": BitgetWsFeed,
                "gate": GateWsFeed,
                "okx": OkxWsFeed,
            }
            exchange_name_to_enum = {
                "binance": ExchangeName.BINANCE,
                "bybit": ExchangeName.BYBIT,
                "bitget": ExchangeName.BITGET,
                "gate": ExchangeName.GATE,
                "okx": ExchangeName.OKX,
            }
            exchange_names = list(TRACKED_EXCHANGES)
            log.info(
                "starting spread lifecycle collector | exchanges=%s | symbols=%d | threshold=%.4f%% | cooldown=%.2fs | db=%s",
                exchange_names,
                len(symbols),
                self.threshold_pct,
                self.cooldown_sec,
                self.db_path,
            )

            tasks: list[asyncio.Task[Any]] = []
            for exchange in TRACKED_EXCHANGES:
                feed_cls = ws_factory[exchange]
                feed = feed_cls(
                    session=session,
                    symbols=symbols,
                    on_quote=self._on_quote,
                )
                tasks.append(
                    asyncio.create_task(
                        feed.run(self.stop_event),
                        name=f"spread-lifecycle-ws-{exchange_name_to_enum[exchange].value}",
                    )
                )

            tasks.append(asyncio.create_task(self._stats_loop(), name="spread-lifecycle-stats"))
            tasks.append(asyncio.create_task(self._cooldown_loop(), name="spread-lifecycle-cooldown"))
            if self.max_hours > 0:
                tasks.append(asyncio.create_task(self._runtime_limit_loop(), name="spread-lifecycle-runtime"))

            await self.stop_event.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        ended_at = datetime.now(UTC)
        self.tracker.close_all_active(ended_at)
        self.db.close()
        runtime_min = max((ended_at - self._run_started_at).total_seconds() / 60.0, 1e-9)
        log.info(
            "collector stopped | runtime_min=%.2f | events_recorded=%d | avg_events_per_min=%.2f",
            runtime_min,
            self.tracker.total_events_recorded,
            self.tracker.total_events_recorded / runtime_min,
        )

    def stop(self) -> None:
        self.stop_event.set()

    def _on_quote(self, quote: Any) -> None:
        exchange_name = getattr(quote.exchange, "value", str(quote.exchange)).lower()
        if exchange_name not in TRACKED_EXCHANGES:
            return
        symbol_quotes = self.latest_quotes_by_symbol.setdefault(quote.symbol, {})
        symbol_quotes[exchange_name] = quote
        self._recompute_symbol_spreads(quote.symbol)

    async def _build_symbol_universe(self, session: aiohttp.ClientSession) -> list[str]:
        from spread_arb.symbol_rotator import discover_candidates

        base_symbols = {str(symbol).upper() for symbol in self.settings.symbols}
        dynamic_symbols: set[str] = set()
        if self.include_dynamic:
            try:
                discovered = await discover_candidates(
                    session=session,
                    base_symbols=base_symbols,
                    min_spread_pct=self.settings.dynamic_min_spread_pct,
                    max_bbo_bps=self.settings.dynamic_max_bbo_bps,
                    min_exchanges=self.settings.dynamic_require_exchanges,
                    max_symbols=self.settings.dynamic_max_symbols,
                )
                dynamic_symbols = set(discovered)
                log.info("dynamic symbols discovered=%d", len(dynamic_symbols))
            except Exception as exc:  # noqa: BLE001
                log.warning("dynamic discovery failed: %s", exc)
        all_symbols = sorted(base_symbols | dynamic_symbols)
        log.info(
            "symbol universe built | base=%d dynamic=%d total=%d",
            len(base_symbols),
            len(dynamic_symbols),
            len(all_symbols),
        )
        return all_symbols

    def _recompute_symbol_spreads(self, symbol: str) -> None:
        now = datetime.now(UTC)
        symbol_quotes = self.latest_quotes_by_symbol.get(symbol)
        if symbol_quotes is None:
            return

        fresh_quotes: dict[str, Any] = {}
        for exchange, quote in symbol_quotes.items():
            if (now - quote.received_at).total_seconds() <= FRESH_QUOTE_MAX_AGE_SEC:
                fresh_quotes[exchange] = quote

        if len(fresh_quotes) < 2:
            return

        for long_exchange_name in TRACKED_EXCHANGES:
            long_quote = fresh_quotes.get(long_exchange_name)
            if long_quote is None:
                continue
            ask_long = float(long_quote.best_ask_price)
            if ask_long <= 0:
                continue
            bbo_long_bps = compute_quote_bbo_bps(long_quote)

            for short_exchange_name in TRACKED_EXCHANGES:
                if short_exchange_name == long_exchange_name:
                    continue
                short_quote = fresh_quotes.get(short_exchange_name)
                if short_quote is None:
                    continue
                bid_short = float(short_quote.best_bid_price)
                if bid_short <= 0:
                    continue
                bbo_short_bps = compute_quote_bbo_bps(short_quote)
                spread_pct = compute_directional_spread_pct(ask_long, bid_short)
                self.tracker.process_update(
                    symbol=symbol,
                    long_exchange=long_exchange_name,
                    short_exchange=short_exchange_name,
                    spread_pct=spread_pct,
                    ts=now,
                    bid_short=bid_short,
                    ask_long=ask_long,
                    bbo_long_bps=bbo_long_bps,
                    bbo_short_bps=bbo_short_bps,
                )

    async def _cooldown_loop(self) -> None:
        while not self.stop_event.is_set():
            self.tracker.close_expired_cooldowns(datetime.now(UTC))
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=COOLDOWN_SWEEP_INTERVAL_SEC)
                return
            except TimeoutError:
                continue

    async def _stats_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=STATS_INTERVAL_SEC)
                return
            except TimeoutError:
                pass

            now = datetime.now(UTC)
            runtime_min = max((now - self._run_started_at).total_seconds() / 60.0, 1e-9)
            rate = self.tracker.total_events_recorded / runtime_min
            top_active = self.tracker.top_active_events(top_n=3)
            if top_active:
                active_line = " | ".join(
                    f"{evt.symbol} {evt.long_exchange}->{evt.short_exchange} {evt.last_spread_pct:+.4f}%"
                    for evt in top_active
                )
            else:
                active_line = "none"
            log.info(
                "stats | active_events=%d | total_events=%d | events_per_min=%.2f | top_active=%s",
                len(self.tracker.active_events),
                self.tracker.total_events_recorded,
                rate,
                active_line,
            )

    async def _runtime_limit_loop(self) -> None:
        max_seconds = self.max_hours * 3600.0
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=max_seconds)
        except TimeoutError:
            log.info("max-hours reached (%.2f), shutting down", self.max_hours)
            self.stop()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def _request_shutdown(sig_name: str) -> None:
            log.info("received %s, shutting down", sig_name)
            self.stop()

        for sig in _supported_signals():
            try:
                loop.add_signal_handler(sig, lambda s=sig: _request_shutdown(s.name))
            except NotImplementedError:
                pass


def _supported_signals() -> tuple[signal.Signals, ...]:
    values: list[signal.Signals] = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        values.append(signal.SIGTERM)
    return tuple(values)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spread lifecycle collector (WebSocket observer)")
    parser.add_argument("--threshold", type=float, default=0.10, help="Start-event spread threshold in percent")
    parser.add_argument("--db", type=Path, default=Path("data/spread_lifecycle.sqlite3"), help="SQLite DB path")
    parser.add_argument("--include-dynamic", action="store_true", help="Include dynamic rotation symbols")
    parser.add_argument("--cooldown", type=float, default=3.0, help="Close cooldown in seconds")
    parser.add_argument("--max-hours", type=float, default=0.0, help="Stop after N hours (0 = unlimited)")
    return parser.parse_args()


async def _main_async(args: argparse.Namespace) -> None:
    collector = SpreadLifecycleCollector(
        threshold_pct=float(args.threshold),
        db_path=Path(args.db),
        include_dynamic=bool(args.include_dynamic),
        cooldown_sec=float(args.cooldown),
        max_hours=float(args.max_hours),
    )
    await collector.run()


def main() -> None:
    args = parse_args()
    from spread_arb.config import get_settings
    from spread_arb.logging_setup import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
