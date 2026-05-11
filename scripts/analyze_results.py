from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _sqlite_path_from_url(database_url: str) -> Path:
    prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
    for prefix in prefixes:
        if database_url.startswith(prefix):
            return Path(database_url.removeprefix(prefix))
    raise ValueError(f"Unsupported DATABASE_URL format: {database_url!r}")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _safe_scalar(conn: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> float:
    row = conn.execute(query, params).fetchone()
    if row is None or row[0] is None:
        return 0.0
    return float(row[0])


def _print_section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def main() -> None:
    database_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/spread_arb.sqlite3")
    db_path = _sqlite_path_from_url(database_url)

    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print("Spread Arb Scanner - Analysis Report")
    print(f"DB: {db_path.resolve()}")

    if not _table_exists(conn, "opportunities"):
        print("\nNo opportunities table found.")
    else:
        _print_section("Opportunities")

        total_opportunities = int(_safe_scalar(conn, "SELECT COUNT(*) FROM opportunities"))
        observed_count = int(_safe_scalar(conn, "SELECT COUNT(*) FROM opportunities WHERE status='observed'"))
        rejected_count = int(_safe_scalar(conn, "SELECT COUNT(*) FROM opportunities WHERE status='rejected'"))
        opened_count = int(_safe_scalar(conn, "SELECT COUNT(*) FROM opportunities WHERE status='opened'"))
        missed_count = int(_safe_scalar(conn, "SELECT COUNT(*) FROM opportunities WHERE status='missed'"))

        observed_rejected_ratio = (
            (observed_count / rejected_count) if rejected_count > 0 else float("inf") if observed_count > 0 else 0.0
        )

        print(f"Total opportunities: {total_opportunities}")
        if observed_rejected_ratio == float("inf"):
            ratio_text = "inf"
        else:
            ratio_text = f"{observed_rejected_ratio:.4f}"
        print(f"Observed vs rejected ratio: {ratio_text}")
        print(f"Opened opportunities: {opened_count}")
        print(f"Missed opportunities: {missed_count}")

        print("\nCount by status/reason:")
        rows = conn.execute(
            """
            SELECT status, COALESCE(reason, '(none)') AS reason, COUNT(*) AS cnt
            FROM opportunities
            GROUP BY status, COALESCE(reason, '(none)')
            ORDER BY cnt DESC, status, reason
            """
        ).fetchall()
        if not rows:
            print("  (no rows)")
        else:
            for row in rows:
                print(f"  {row['status']:<10} | {row['reason']:<35} | {row['cnt']}")

    if not _table_exists(conn, "paper_trades"):
        print("\nNo paper_trades table found.")
    else:
        _print_section("Paper Trades")

        paper_count = int(_safe_scalar(conn, "SELECT COUNT(*) FROM paper_trades"))
        total_gross = _safe_scalar(conn, "SELECT SUM(gross_pnl_usdt) FROM paper_trades")
        total_fees = _safe_scalar(conn, "SELECT SUM(fees_usdt) FROM paper_trades")
        total_slippage = _safe_scalar(conn, "SELECT SUM(slippage_usdt) FROM paper_trades")
        total_net = _safe_scalar(conn, "SELECT SUM(net_pnl_usdt) FROM paper_trades")
        avg_net = _safe_scalar(conn, "SELECT AVG(net_pnl_usdt) FROM paper_trades")
        avg_hold = _safe_scalar(conn, "SELECT AVG(hold_seconds) FROM paper_trades")
        wins = int(_safe_scalar(conn, "SELECT COUNT(*) FROM paper_trades WHERE net_pnl_usdt > 0"))
        winrate = (wins / paper_count * 100.0) if paper_count > 0 else 0.0

        print(f"Paper trades count: {paper_count}")
        print(f"Total gross PnL (USDT): {total_gross:.6f}")
        print(f"Total fees (USDT): {total_fees:.6f}")
        print(f"Total slippage (USDT): {total_slippage:.6f}")
        print(f"Total net PnL (USDT): {total_net:.6f}")
        print(f"Winrate: {winrate:.2f}%")
        print(f"Average net PnL per trade (USDT): {avg_net:.6f}")
        print(f"Average hold time (sec): {avg_hold:.2f}")

        print("\nClose reason breakdown:")
        close_rows = conn.execute(
            """
            SELECT close_reason, COUNT(*) AS cnt
            FROM paper_trades
            GROUP BY close_reason
            ORDER BY cnt DESC, close_reason
            """
        ).fetchall()
        if not close_rows:
            print("  (no rows)")
        else:
            for row in close_rows:
                print(f"  {row['close_reason']:<24} | {row['cnt']}")

        print("\nBest symbols by net PnL:")
        best_symbols = conn.execute(
            """
            SELECT symbol, SUM(net_pnl_usdt) AS net_pnl, COUNT(*) AS trades
            FROM paper_trades
            GROUP BY symbol
            ORDER BY net_pnl DESC
            LIMIT 5
            """
        ).fetchall()
        if not best_symbols:
            print("  (no rows)")
        else:
            for row in best_symbols:
                print(f"  {row['symbol']:<10} | net={row['net_pnl']:.6f} | trades={row['trades']}")

        print("\nWorst symbols by net PnL:")
        worst_symbols = conn.execute(
            """
            SELECT symbol, SUM(net_pnl_usdt) AS net_pnl, COUNT(*) AS trades
            FROM paper_trades
            GROUP BY symbol
            ORDER BY net_pnl ASC
            LIMIT 5
            """
        ).fetchall()
        if not worst_symbols:
            print("  (no rows)")
        else:
            for row in worst_symbols:
                print(f"  {row['symbol']:<10} | net={row['net_pnl']:.6f} | trades={row['trades']}")

        print("\nBest exchange pairs by net PnL:")
        best_pairs = conn.execute(
            """
            SELECT long_exchange || '->' || short_exchange AS pair, SUM(net_pnl_usdt) AS net_pnl, COUNT(*) AS trades
            FROM paper_trades
            GROUP BY long_exchange, short_exchange
            ORDER BY net_pnl DESC
            LIMIT 5
            """
        ).fetchall()
        if not best_pairs:
            print("  (no rows)")
        else:
            for row in best_pairs:
                print(f"  {row['pair']:<20} | net={row['net_pnl']:.6f} | trades={row['trades']}")

        print("\nWorst exchange pairs by net PnL:")
        worst_pairs = conn.execute(
            """
            SELECT long_exchange || '->' || short_exchange AS pair, SUM(net_pnl_usdt) AS net_pnl, COUNT(*) AS trades
            FROM paper_trades
            GROUP BY long_exchange, short_exchange
            ORDER BY net_pnl ASC
            LIMIT 5
            """
        ).fetchall()
        if not worst_pairs:
            print("  (no rows)")
        else:
            for row in worst_pairs:
                print(f"  {row['pair']:<20} | net={row['net_pnl']:.6f} | trades={row['trades']}")

    conn.close()


if __name__ == "__main__":
    main()

