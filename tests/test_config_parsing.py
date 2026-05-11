from pathlib import Path
from uuid import uuid4

from spread_arb.config import Settings


def _write_env(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _make_local_env_file() -> Path:
    folder = Path("tests") / ".tmp_env"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{uuid4().hex}.env"


def test_csv_parsing_for_exchanges_and_symbols() -> None:
    env_file = _make_local_env_file()
    try:
        _write_env(
            env_file,
            "\n".join(
                [
                    "EXCHANGES= mexc, bybit, , MEXC ",
                    "SYMBOLS= solusdt, XRPUSDT, , dogeusdt ",
                ]
            ),
        )

        settings = Settings(_env_file=env_file)

        assert [item.value for item in settings.exchanges] == ["mexc", "bybit", "mexc"]
        assert settings.symbols == ["SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    finally:
        env_file.unlink(missing_ok=True)


def test_json_list_parsing_for_exchanges_and_symbols() -> None:
    env_file = _make_local_env_file()
    try:
        _write_env(
            env_file,
            "\n".join(
                [
                    'EXCHANGES=["MEXC", "bybit"]',
                    'SYMBOLS=["solusdt", "ethusdt"]',
                ]
            ),
        )

        settings = Settings(_env_file=env_file)

        assert [item.value for item in settings.exchanges] == ["mexc", "bybit"]
        assert settings.symbols == ["SOLUSDT", "ETHUSDT"]
    finally:
        env_file.unlink(missing_ok=True)
