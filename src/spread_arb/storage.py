from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _sqlite_path_from_url(database_url: str) -> Path:
    prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
    for prefix in prefixes:
        if database_url.startswith(prefix):
            return Path(database_url.removeprefix(prefix))
    raise ValueError(f"Unsupported DATABASE_URL format: {database_url!r}")


@dataclass(slots=True)
class OpportunityRecord:
    timestamp: str
    symbol: str
    long_exchange: str
    short_exchange: str
    ask_long: float
    bid_short: float
    raw_spread_pct: float
    estimated_net_spread_pct: float
    estimated_roundtrip_cost_pct: float
    available_long_size: float
    available_short_size: float
    quote_age_ms_long: float
    quote_age_ms_short: float
    notional_usdt: float
    status: str
    reason: str | None
    created_at: str


@dataclass(slots=True)
class SpreadSnapshotRecord:
    timestamp: str
    symbol: str
    exchange_a: str
    exchange_b: str
    bid_a: float
    ask_a: float
    bid_b: float
    ask_b: float
    raw_spread_ab_pct: float   # long A (buy ask_a), short B (sell bid_b)
    raw_spread_ba_pct: float   # long B (buy ask_b), short A (sell bid_a)
    best_raw_spread_pct: float  # max of the two directions
    quote_age_a_ms: float
    quote_age_b_ms: float


@dataclass(slots=True)
class PaperTradeRecord:
    symbol: str
    long_exchange: str
    short_exchange: str
    notional_usdt: float
    opened_at: str
    closed_at: str
    hold_seconds: float
    entry_long_price: float
    entry_short_price: float
    exit_long_price: float
    exit_short_price: float
    entry_raw_spread_pct: float
    exit_raw_spread_pct: float
    gross_pnl_usdt: float
    fees_usdt: float
    slippage_usdt: float
    funding_usdt: float
    net_pnl_usdt: float
    net_pnl_pct: float
    close_reason: str
    max_adverse_spread_pct: float
    max_favorable_spread_pct: float
    created_at: str


def init_sqlite(database_url: str) -> Path:
    db_path = _sqlite_path_from_url(database_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                best_bid_price TEXT NOT NULL,
                best_bid_size TEXT NOT NULL,
                best_ask_price TEXT NOT NULL,
                best_ask_size TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                long_exchange TEXT NOT NULL,
                short_exchange TEXT NOT NULL,
                ask_long REAL NOT NULL,
                bid_short REAL NOT NULL,
                raw_spread_pct REAL NOT NULL,
                estimated_net_spread_pct REAL NOT NULL,
                estimated_roundtrip_cost_pct REAL NOT NULL,
                available_long_size REAL NOT NULL,
                available_short_size REAL NOT NULL,
                quote_age_ms_long REAL NOT NULL,
                quote_age_ms_short REAL NOT NULL,
                notional_usdt REAL NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_opportunities_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                long_exchange TEXT NOT NULL,
                short_exchange TEXT NOT NULL,
                notional_usdt REAL NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                hold_seconds REAL NOT NULL,
                entry_long_price REAL NOT NULL,
                entry_short_price REAL NOT NULL,
                exit_long_price REAL NOT NULL,
                exit_short_price REAL NOT NULL,
                entry_raw_spread_pct REAL NOT NULL,
                exit_raw_spread_pct REAL NOT NULL,
                gross_pnl_usdt REAL NOT NULL,
                fees_usdt REAL NOT NULL,
                slippage_usdt REAL NOT NULL,
                funding_usdt REAL NOT NULL,
                net_pnl_usdt REAL NOT NULL,
                net_pnl_pct REAL NOT NULL,
                close_reason TEXT NOT NULL,
                max_adverse_spread_pct REAL NOT NULL,
                max_favorable_spread_pct REAL NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_paper_trades_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spread_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                exchange_a TEXT NOT NULL,
                exchange_b TEXT NOT NULL,
                bid_a REAL NOT NULL,
                ask_a REAL NOT NULL,
                bid_b REAL NOT NULL,
                ask_b REAL NOT NULL,
                raw_spread_ab_pct REAL NOT NULL,
                raw_spread_ba_pct REAL NOT NULL,
                best_raw_spread_pct REAL NOT NULL,
                quote_age_a_ms REAL NOT NULL,
                quote_age_b_ms REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_pair
            ON spread_snapshots (symbol, exchange_a, exchange_b)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp
            ON spread_snapshots (timestamp)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                level TEXT NOT NULL,
                source TEXT NOT NULL,
                message TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                exchange TEXT NOT NULL,
                total_usdt REAL NOT NULL,
                available_usdt REAL NOT NULL,
                snapshot_type TEXT NOT NULL DEFAULT 'periodic'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_balance_snapshots_ts_exchange
            ON balance_snapshots (timestamp, exchange)
            """
        )
        conn.commit()

    return db_path


def _ensure_opportunities_columns(conn: sqlite3.Connection) -> None:
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(opportunities)").fetchall()
    }

    required: dict[str, str] = {
        "timestamp": "TEXT",
        "symbol": "TEXT",
        "long_exchange": "TEXT",
        "short_exchange": "TEXT",
        "ask_long": "REAL",
        "bid_short": "REAL",
        "raw_spread_pct": "REAL",
        "estimated_net_spread_pct": "REAL",
        "estimated_roundtrip_cost_pct": "REAL",
        "available_long_size": "REAL",
        "available_short_size": "REAL",
        "quote_age_ms_long": "REAL",
        "quote_age_ms_short": "REAL",
        "notional_usdt": "REAL",
        "status": "TEXT",
        "reason": "TEXT",
        "created_at": "TEXT",
    }

    for column, sql_type in required.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE opportunities ADD COLUMN {column} {sql_type}")


def _ensure_paper_trades_columns(conn: sqlite3.Connection) -> None:
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()
    }

    required: dict[str, str] = {
        "id": "INTEGER",
        "symbol": "TEXT",
        "long_exchange": "TEXT",
        "short_exchange": "TEXT",
        "notional_usdt": "REAL",
        "opened_at": "TEXT",
        "closed_at": "TEXT",
        "hold_seconds": "REAL",
        "entry_long_price": "REAL",
        "entry_short_price": "REAL",
        "exit_long_price": "REAL",
        "exit_short_price": "REAL",
        "entry_raw_spread_pct": "REAL",
        "exit_raw_spread_pct": "REAL",
        "gross_pnl_usdt": "REAL",
        "fees_usdt": "REAL",
        "slippage_usdt": "REAL",
        "funding_usdt": "REAL",
        "net_pnl_usdt": "REAL",
        "net_pnl_pct": "REAL",
        "close_reason": "TEXT",
        "max_adverse_spread_pct": "REAL",
        "max_favorable_spread_pct": "REAL",
        "created_at": "TEXT",
    }

    for column, sql_type in required.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {column} {sql_type}")


class OpportunityStore:
    def __init__(self, database_url: str) -> None:
        self.db_path = _sqlite_path_from_url(database_url)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> OpportunityStore:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def insert_opportunity(self, record: OpportunityRecord) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO opportunities (
                timestamp,
                symbol,
                long_exchange,
                short_exchange,
                ask_long,
                bid_short,
                raw_spread_pct,
                estimated_net_spread_pct,
                estimated_roundtrip_cost_pct,
                available_long_size,
                available_short_size,
                quote_age_ms_long,
                quote_age_ms_short,
                notional_usdt,
                status,
                reason,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.timestamp,
                record.symbol,
                record.long_exchange,
                record.short_exchange,
                record.ask_long,
                record.bid_short,
                record.raw_spread_pct,
                record.estimated_net_spread_pct,
                record.estimated_roundtrip_cost_pct,
                record.available_long_size,
                record.available_short_size,
                record.quote_age_ms_long,
                record.quote_age_ms_short,
                record.notional_usdt,
                record.status,
                record.reason,
                record.created_at,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def update_opportunity_status(self, opportunity_id: int, status: str, reason: str | None) -> None:
        self.conn.execute(
            """
            UPDATE opportunities
            SET status = ?, reason = ?
            WHERE id = ?
            """,
            (status, reason, opportunity_id),
        )
        self.conn.commit()

    def insert_paper_trade(self, record: PaperTradeRecord) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO paper_trades (
                symbol,
                long_exchange,
                short_exchange,
                notional_usdt,
                opened_at,
                closed_at,
                hold_seconds,
                entry_long_price,
                entry_short_price,
                exit_long_price,
                exit_short_price,
                entry_raw_spread_pct,
                exit_raw_spread_pct,
                gross_pnl_usdt,
                fees_usdt,
                slippage_usdt,
                funding_usdt,
                net_pnl_usdt,
                net_pnl_pct,
                close_reason,
                max_adverse_spread_pct,
                max_favorable_spread_pct,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.symbol,
                record.long_exchange,
                record.short_exchange,
                record.notional_usdt,
                record.opened_at,
                record.closed_at,
                record.hold_seconds,
                record.entry_long_price,
                record.entry_short_price,
                record.exit_long_price,
                record.exit_short_price,
                record.entry_raw_spread_pct,
                record.exit_raw_spread_pct,
                record.gross_pnl_usdt,
                record.fees_usdt,
                record.slippage_usdt,
                record.funding_usdt,
                record.net_pnl_usdt,
                record.net_pnl_pct,
                record.close_reason,
                record.max_adverse_spread_pct,
                record.max_favorable_spread_pct,
                record.created_at,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def insert_spread_snapshots(self, records: list[SpreadSnapshotRecord]) -> int:
        """Batch-insert spread snapshots. Returns number of rows inserted."""
        if not records:
            return 0
        self.conn.executemany(
            """
            INSERT INTO spread_snapshots (
                timestamp, symbol, exchange_a, exchange_b,
                bid_a, ask_a, bid_b, ask_b,
                raw_spread_ab_pct, raw_spread_ba_pct, best_raw_spread_pct,
                quote_age_a_ms, quote_age_b_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.timestamp, r.symbol, r.exchange_a, r.exchange_b,
                    r.bid_a, r.ask_a, r.bid_b, r.ask_b,
                    r.raw_spread_ab_pct, r.raw_spread_ba_pct, r.best_raw_spread_pct,
                    r.quote_age_a_ms, r.quote_age_b_ms,
                )
                for r in records
            ],
        )
        self.conn.commit()
        return len(records)

    async def save_balance_snapshot(
        self,
        exchange: str,
        total_usdt: float,
        available_usdt: float,
        snapshot_type: str = "periodic",
    ) -> None:
        """Insert a balance snapshot row."""
        self.conn.execute(
            """
            INSERT INTO balance_snapshots (
                timestamp,
                exchange,
                total_usdt,
                available_usdt,
                snapshot_type
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                self.now_iso(),
                exchange,
                total_usdt,
                available_usdt,
                snapshot_type,
            ),
        )
        self.conn.commit()

    async def get_balance_snapshots(
        self,
        since: str | None = None,
        exchange: str | None = None,
    ) -> list[dict]:
        """Retrieve balance snapshots, optionally filtered by time and exchange."""
        query = """
            SELECT
                id,
                timestamp,
                exchange,
                total_usdt,
                available_usdt,
                snapshot_type
            FROM balance_snapshots
            WHERE 1=1
        """
        params: list[str] = []
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(since)
        if exchange is not None:
            query += " AND exchange = ?"
            params.append(exchange)
        query += " ORDER BY timestamp ASC, id ASC"

        cursor = self.conn.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        return [dict(zip(columns, row, strict=False)) for row in rows]

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).isoformat()
