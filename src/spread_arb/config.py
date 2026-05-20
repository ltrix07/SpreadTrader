from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import ExchangeName, Symbol


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        enable_decoding=False,
    )

    app_env: str = "dev"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///data/spread_arb.sqlite3"

    exchanges: list[ExchangeName] = Field(default_factory=lambda: [
        ExchangeName.OKX, ExchangeName.BYBIT, ExchangeName.BINANCE,
        ExchangeName.GATE, ExchangeName.BITGET, ExchangeName.HTX,
    ])
    market_type: str = "perp"
    symbols: list[Symbol] = Field(default_factory=lambda: [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
        "ADAUSDT", "AVAXUSDT", "LINKUSDT", "APTUSDT", "ARBUSDT",
        "OPUSDT", "SUIUSDT", "SEIUSDT", "WIFUSDT", "PEPEUSDT",
        "FLOKIUSDT", "INJUSDT", "NEARUSDT", "ORDIUSDT",
        "1000BONKUSDT",
        "DOTUSDT", "ATOMUSDT", "FILUSDT", "UNIUSDT", "LDOUSDT",
        "AAVEUSDT", "GRTUSDT", "RUNEUSDT", "TIAUSDT", "STXUSDT",
        "FETUSDT", "PENDLEUSDT", "JUPUSDT", "ENAUSDT", "ONDOUSDT",
        "ZKUSDT", "STRKUSDT", "BLURUSDT", "DYDXUSDT", "GALAUSDT",
        "CFXUSDT", "IMXUSDT", "GMXUSDT", "MASKUSDT", "WOOUSDT",
        "ACHUSDT", "CELOUSDT", "LRCUSDT", "SKLUSDT", "ZENUSDT",
    ])

    paper_notional_usdt: float = Field(default=100.0, gt=0)
    max_open_positions: int = Field(default=3, ge=1)
    one_position_per_symbol: bool = True
    symbol_cooldown_sec: int = Field(default=60, ge=0)
    min_raw_spread_pct: float = Field(default=0.45)
    entry_net_spread_pct: float = Field(default=0.25)
    exit_spread_pct: float = Field(default=0.05, ge=0)
    stop_spread_pct: float = Field(default=0.80, ge=0)
    max_hold_seconds: int = Field(default=900, ge=1)
    simulated_execution_delay_ms: int = Field(default=500, ge=0)
    max_quote_age_ms: int = Field(default=2000, ge=1)
    slippage_buffer_pct: float = Field(default=0.01, ge=0)
    safety_buffer_pct: float = Field(default=0.01, ge=0)

    taker_fee_mexc_pct: float = Field(default=0.05, ge=0)
    taker_fee_bybit_pct: float = Field(default=0.055, ge=0)
    taker_fee_binance_pct: float = Field(default=0.05, ge=0)
    taker_fee_okx_pct: float = Field(default=0.05, ge=0)
    taker_fee_gate_pct: float = Field(default=0.05, ge=0)
    taker_fee_bitget_pct: float = Field(default=0.05, ge=0)
    taker_fee_htx_pct: float = Field(default=0.05, ge=0)

    # Mean reversion settings
    mr_enabled: bool = True
    mr_sigma_entry: float = Field(default=3.0, gt=0)
    mr_sigma_stop: float = Field(default=6.0, gt=0)
    mr_min_stop_distance_pct: float = Field(default=0.15, ge=0)
    mr_rolling_window: int = Field(default=360, ge=30)
    mr_min_net_edge_pct: float = Field(default=0.40, ge=0)
    mr_max_positions: int = Field(default=1, ge=1)
    mr_notional_usdt: float = Field(default=350.0, gt=0)
    mr_compound_enabled: bool = True
    mr_notional_pct: float = Field(default=85.0, gt=0, le=100)
    mr_min_notional_usdt: float = Field(default=5.0, ge=1)
    mr_max_hold_seconds: int = Field(default=900, ge=1)
    mr_cooldown_sec: int = Field(default=30, ge=0)
    mr_exit_max_quote_age_ms: int = Field(default=30_000, ge=1)
    mr_take_profit_fraction: float = Field(default=0.75, gt=0, le=1.0)
    mr_excluded_exchanges: list[str] = Field(default_factory=lambda: ["htx"])
    mr_quote_freshness_window: int = Field(default=30, ge=5)
    mr_min_quote_freshness_pct: float = Field(default=80.0, ge=0, le=100)

    use_websocket: bool = True

    poll_interval_sec: float = Field(default=1.0, gt=0)
    request_timeout_sec: float = Field(default=8.0, gt=0)
    reconnect_backoff_sec: float = Field(default=2.0, gt=0)
    top_spreads_log_interval_sec: float = Field(default=5.0, gt=0)
    spread_scan_interval_sec: float = Field(default=0.5, gt=0)

    # Live trading
    live_trading: bool = False
    default_leverage: int = Field(default=3, ge=1, le=125)
    order_timeout_sec: float = Field(default=10.0, gt=0)
    max_notional_usdt: float = Field(default=50.0, ge=0)
    balance_snapshot_interval_sec: int = Field(default=1800, ge=60)
    exchange_stop_loss_pct: float = Field(default=5.0, gt=0, le=50)

    # API credentials
    api_key_binance: str = ""
    api_secret_binance: str = ""
    api_key_okx: str = ""
    api_secret_okx: str = ""
    api_passphrase_okx: str = ""
    api_key_bybit: str = ""
    api_secret_bybit: str = ""
    api_key_bitget: str = ""
    api_secret_bitget: str = ""
    api_passphrase_bitget: str = ""
    api_key_mexc: str = ""
    api_secret_mexc: str = ""

    @staticmethod
    def _parse_list_env(value: Any) -> list[str] | Any:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("[") and raw.endswith("]"):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = raw
                else:
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in raw.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return value

    @field_validator("exchanges", mode="before")
    @classmethod
    def _parse_exchanges(cls, value: Any) -> Any:
        parsed = cls._parse_list_env(value)
        if isinstance(parsed, list):
            return [item.lower() for item in parsed]
        return parsed

    @field_validator("symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, value: Any) -> Any:
        parsed = cls._parse_list_env(value)
        if isinstance(parsed, list):
            return [item.upper() for item in parsed]
        return parsed

    @field_validator("mr_excluded_exchanges", mode="before")
    @classmethod
    def _parse_mr_excluded_exchanges(cls, value: Any) -> Any:
        parsed = cls._parse_list_env(value)
        if isinstance(parsed, list):
            return [item.lower() for item in parsed]
        return parsed


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
