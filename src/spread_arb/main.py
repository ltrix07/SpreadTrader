from __future__ import annotations

import argparse
import asyncio
import logging

from .config import get_settings
from .logging_setup import configure_logging
from .scanner import QuoteScanner
from .storage import init_sqlite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spread-arb")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run public market-data scanner")
    run_parser.set_defaults(func=run_command)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func()


def run_command() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    logger.info(
        "effective config | exchanges=%s | symbols=%s",
        [exchange.value for exchange in settings.exchanges],
        settings.symbols,
    )

    db_path = init_sqlite(settings.database_url)
    logger.info("sqlite initialized at %s", db_path)

    scanner = QuoteScanner(settings)
    try:
        asyncio.run(scanner.run())
    except KeyboardInterrupt:
        logger.info("received Ctrl+C, stopping")


if __name__ == "__main__":
    main()
