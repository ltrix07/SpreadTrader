# Historical backtester for MeanReversionEngine

## Контекст

У нас уже есть `MeanReversionEngine` (`src/spread_arb/mean_reversion_engine.py`) который работает в paper и live режиме. Цель: построить **бэктестер** который прогоняет ровно тот же engine на исторических данных из `spread_snapshots`, чтобы можно было тюнить параметры без потери реальных денег.

**Ключевой принцип**: переиспользовать `MeanReversionEngine` максимально. Никакого дубликата стратегии. Если backtester и live дают разные результаты — это баг бэктестера, не другой engine.

Данные доступны в `data/spread_arb_proj.sqlite3` (на сервере, 2.6 GB, ~10M+ строк за пару месяцев). Схема таблицы `spread_snapshots`:

```
id INTEGER PRIMARY KEY,
timestamp TEXT,         -- ISO 8601 UTC
symbol TEXT,
exchange_a TEXT,
exchange_b TEXT,
bid_a REAL, ask_a REAL,
bid_b REAL, ask_b REAL,
raw_spread_ab_pct REAL,
raw_spread_ba_pct REAL,
best_raw_spread_pct REAL,
quote_age_a_ms REAL,
quote_age_b_ms REAL
```

**Чего нет в snapshot'ах**: `best_bid_size` / `best_ask_size`. Это значит liquidity capacity filter (`mr_min_top_capacity_multiplier × notional`) симулировать нельзя. В V1 отключаем его: при симуляции считаем размеры BBO «достаточными» (как будто `best_ask_size × best_ask_price >> required_capacity`). Это потеря точности на тонких маркетах, но большинство наших символов прилично ликвидные — погрешность приемлема.

## Файлы для изменения / создания

1. **`src/spread_arb/mean_reversion_engine.py`** — минимальный refactor для clock injection.
2. **`scripts/backtest.py`** — новый файл, основной runner.
3. **`scripts/backtest_report.py`** — новый файл, парсер CSV → текстовый отчёт.
4. **`tests/test_backtest_clock.py`** — тест для clock injection (чтобы убедиться что live не сломался).

**Не трогать**: `execution.py`, `scanner.py`, `storage.py`, `paper_engine.py`, exchange clients.

## Часть 1: Clock injection в `MeanReversionEngine`

### Проблема

`MeanReversionEngine` сейчас вызывает `datetime.now(UTC)` напрямую в нескольких местах (`update_baselines`, `check_exits`, `_execute_after_delay`, `_close_position`). Для бэктеста надо чтобы «сейчас» было `snapshot.timestamp`, а не реальное системное время.

### Что сделать

В `MeanReversionEngine.__init__` добавить опциональный параметр:

```python
def __init__(
    self,
    *,
    settings: Settings,
    opportunity_store: OpportunityStore,
    get_latest_quote: Callable[[ExchangeName, str], Quote | None],
    execution_service: ExecutionService | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    ...
    self._clock = clock or (lambda: datetime.now(UTC))
```

Все вызовы `datetime.now(UTC)` внутри методов engine заменить на `self._clock()`. Это примерно 5 мест:

- `update_baselines` (строка ~478): `now = datetime.now(UTC)` → `now = self._clock()`
- `check_exits` (строка ~539): то же
- `_execute_after_delay` (строки ~868): то же
- `_close_position` (строка ~1008): то же

**Не трогать** `OpportunityStore.now_iso()` — там не клок-зависимая логика, это просто timestamp в БД для записи. Если в бэктесте записи в БД не нужны (используем DummyStore), это не проблема.

Существующие тесты не должны сломаться — default `clock=None` даёт прежнее поведение через `datetime.now(UTC)`.

### Тест: `tests/test_backtest_clock.py`

Маленький тест что инжектированный clock используется:

```python
from datetime import UTC, datetime
from spread_arb.config import Settings
from spread_arb.mean_reversion_engine import MeanReversionEngine

def test_clock_injection_used_by_check_exits():
    fixed_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    settings = Settings(symbols=["BTCUSDT"])

    class DummyStore:
        def insert_paper_trade(self, _record): pass

    engine = MeanReversionEngine(
        settings=settings,
        opportunity_store=DummyStore(),
        get_latest_quote=lambda _e, _s: None,
        clock=lambda: fixed_time,
    )
    assert engine._clock() == fixed_time

def test_clock_defaults_to_utc_now():
    settings = Settings(symbols=["BTCUSDT"])

    class DummyStore:
        def insert_paper_trade(self, _record): pass

    engine = MeanReversionEngine(
        settings=settings,
        opportunity_store=DummyStore(),
        get_latest_quote=lambda _e, _s: None,
    )
    # Default clock returns real time within reasonable bounds
    from datetime import datetime, UTC
    before = datetime.now(UTC)
    result = engine._clock()
    after = datetime.now(UTC)
    assert before <= result <= after
```

## Часть 2: `scripts/backtest.py` — основной runner

### Что делает

1. Читает `spread_snapshots` из SQLite по диапазону времени, отсортированно по `timestamp`.
2. Группирует snapshot'ы в «тики» (~1-секундные окна) — для каждого тика собирает все доступные котировки в `snapshot_quotes: dict[(ExchangeName, str), Quote]`.
3. Для каждой параметрической комбинации в grid:
   - Создаёт `MeanReversionEngine` с подмененным `clock` возвращающим текущий tick timestamp.
   - Подменяет `OpportunityStore` на in-memory collector (собирает `PaperTradeRecord` в список).
   - Подменяет `ExecutionService` на None (paper mode — engine сам по `live_mode=False` использует best_ask/best_bid из quotes).
   - Прогоняет все тики через `engine.update_baselines(snapshot_quotes)` и `engine.check_exits(quotes)`.
   - Asyncio task'и для `_execute_after_delay` придётся обработать — см. ниже.
   - Записывает результаты trades + метрики в общий CSV.

### Конкретика реализации

#### Реконструкция Quote из snapshot row

```python
from datetime import UTC, datetime
from decimal import Decimal
from spread_arb.models import ExchangeName, Quote

def _build_quote(*, exchange: ExchangeName, symbol: str, bid: float, ask: float, age_ms: float, timestamp: datetime) -> Quote:
    # received_at must be reconstructed from timestamp - age_ms (we don't have it directly)
    received_at = timestamp - timedelta(milliseconds=age_ms) if age_ms > 0 else timestamp
    return Quote(
        exchange=exchange,
        symbol=symbol,
        best_bid_price=Decimal(str(bid)),
        best_ask_price=Decimal(str(ask)),
        best_bid_size=Decimal("1000000"),  # fake "infinite" size — capacity filter is bypassed
        best_ask_size=Decimal("1000000"),
        receive_latency_ms=0.0,
        source_latency_ms=age_ms,
        received_at=received_at,
    )
```

Для каждой snapshot row создаём ДВЕ Quote (одна для exchange_a, одна для exchange_b), которые потом ложатся в общий dict.

#### Группировка в тики

Snapshot'ы пишутся batch'ами каждые 10 секунд для всех пар одновременно (см. scanner). Поэтому tick = группа snapshot'ов с timestamp в одном секундном окне. Группируем:

```python
import sqlite3
from collections import defaultdict

def _iter_ticks(conn: sqlite3.Connection, since: str, until: str):
    cursor = conn.execute(
        "SELECT timestamp, symbol, exchange_a, exchange_b, bid_a, ask_a, bid_b, ask_b, quote_age_a_ms, quote_age_b_ms "
        "FROM spread_snapshots "
        "WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp ASC",
        (since, until),
    )

    current_tick_ts: str | None = None
    current_tick_quotes: dict[tuple[ExchangeName, str], Quote] = {}

    for row in cursor:
        ts = row[0]
        # Round to second precision for tick grouping
        tick_key = ts[:19]  # "2026-05-25T05:30:00"
        if current_tick_ts is not None and tick_key != current_tick_ts:
            yield _parse_iso(current_tick_ts), current_tick_quotes
            current_tick_quotes = {}
        current_tick_ts = tick_key
        # ... build two Quotes from row, add to current_tick_quotes ...
    if current_tick_ts is not None:
        yield _parse_iso(current_tick_ts), current_tick_quotes
```

#### Обработка asyncio.Task в engine

`_execute_after_delay` создаёт `asyncio.create_task(self._execute_after_delay(symbol))` — это требует event loop. В бэктесте мы можем:

**Подход A (рекомендую)**: запускать backtest внутри `asyncio.run()` и периодически давать loop проиграть pending tasks через `await asyncio.sleep(0)`. Между каждым тиком вставляем await чтобы pending entry tasks могли запуститься. Проблема: реальный `_execute_after_delay` ждёт `mr_revalidation_delay_sec` секунд через `asyncio.sleep(delay_sec)`. В симуляции это «настоящие» секунды, не симулированные.

**Подход B (правильнее)**: в бэктестере подменяем `asyncio.sleep` на fast version, либо устанавливаем `mr_revalidation_delay_sec=0` на время прогона. Engine при `delay_sec=0` сразу проверит conditions и откроет/откажет — это OK с точки зрения логики, потому что снапшоты только каждые 10s, всё равно нет более тонкого разрешения.

**Использовать подход B**: в backtester forced override `settings.mr_revalidation_delay_sec = 0.0` при создании engine. Re-validation logic всё равно отработает (проверка floor, baseline shift, net_edge) на quotes того же тика — это разумная аппроксимация для 10-секундной гранулярности.

Получается псевдо-loop вид:

```python
async def _replay(engine, ticks):
    for tick_ts, quotes in ticks:
        engine.update_baselines(quotes)  # also calls _evaluate_signal which schedules tasks
        engine.check_exits(quotes)
        # Let pending tasks run (entry and close)
        await asyncio.sleep(0)  # yield to scheduler
        # Drain any pending closes that should complete now
        if engine.pending_entries_by_symbol:
            await asyncio.gather(
                *(p.task for p in list(engine.pending_entries_by_symbol.values())),
                return_exceptions=True,
            )
        if engine.pending_closes_by_symbol:
            await asyncio.gather(
                *list(engine.pending_closes_by_symbol.values()),
                return_exceptions=True,
            )
```

#### In-memory PaperTradeRecord collector

```python
from spread_arb.storage import PaperTradeRecord

class InMemoryStore:
    def __init__(self) -> None:
        self.trades: list[PaperTradeRecord] = []

    def insert_paper_trade(self, record: PaperTradeRecord) -> int:
        self.trades.append(record)
        return len(self.trades)

    # Если engine ещё что-то вызывает у store — добавить стабы
    def update_opportunity_status(self, *_args, **_kwargs) -> None: pass
    @staticmethod
    def now_iso() -> str:
        from datetime import datetime, UTC
        return datetime.now(UTC).isoformat()
```

### Grid sweep — sequential

Не делаем грид всех комбинаций (это миллион вариантов). Делаем **sequential sweep**: фиксируем базовые параметры, варьируем один параметр, находим лучший, фиксируем, переходим к следующему.

Конфигурация sweep — встроенная в `backtest.py`:

```python
BASE_PARAMS: dict[str, float | int] = {
    "mr_sigma_entry": 2.5,
    "mr_min_net_edge_pct": 0.30,
    "mr_revalidation_min_spread_pct": 0.60,
    "mr_max_hold_seconds": 300,
    "mr_max_bbo_spread_bps": 8.0,
    "mr_max_baseline_mean_pct": 0.50,
    "mr_take_profit_fraction": 0.75,
    "mr_sigma_stop": 6.0,
    "mr_min_stop_distance_pct": 0.15,
}

SWEEPS: list[tuple[str, list[float | int]]] = [
    ("mr_sigma_entry", [2.0, 2.5, 3.0, 3.5, 4.0]),
    ("mr_min_net_edge_pct", [0.10, 0.20, 0.30, 0.40, 0.50]),
    ("mr_revalidation_min_spread_pct", [0.40, 0.50, 0.60, 0.70, 0.80]),
    ("mr_max_hold_seconds", [180, 300, 450, 600, 900]),
    ("mr_max_bbo_spread_bps", [5.0, 8.0, 10.0, 15.0]),
    ("mr_take_profit_fraction", [0.50, 0.60, 0.75, 0.85]),
    ("mr_sigma_stop", [4.0, 5.0, 6.0, 8.0]),
    ("mr_max_baseline_mean_pct", [0.30, 0.50, 0.70]),
]
```

**Логика**:
- Прогоняем BASE_PARAMS как baseline (один прогон).
- Для каждого sweep'а: берём текущие BASE_PARAMS, варьируем один параметр через список значений. Записываем все варианты в CSV. После завершения sweep'а **обновляем BASE_PARAMS** на лучшее значение по сложной метрике (см. ниже) и переходим к следующему sweep'у.

Итого прогонов: 1 + 5 + 5 + 5 + 5 + 4 + 4 + 4 + 3 = 36 прогонов. Если каждый прогон ~20-40 минут (это грубая оценка для 2 месяцев данных) — это 12-24 часа сквозного прогона. Можно запустить на сервере под `nohup` параллельно с live ботом (read-only к БД).

### Метрика для выбора «лучшего»

Не просто total_pnl (может быть один lucky trade), не просто winrate (можно иметь WR 90% с мизерным PnL). Используем композитную:

```python
def score(metrics: dict) -> float:
    if metrics["trade_count"] < 5:
        return -1e9  # игнорируем слишком малую выборку
    return metrics["total_net_pnl_usdt"] * (0.5 + 0.5 * metrics["winrate"])
```

То есть: total PnL взвешенный по WR. WR ниже 50% штрафует, выше 50% не сильно бонусит — главное чтобы стабильно зарабатывал. Можно тюнить эту функцию, но для V1 достаточно.

### Output CSV

Один файл `backtest_results.csv` в текущей директории. Колонки:

```
run_id,sweep_name,param_name,param_value,
mr_sigma_entry,mr_min_net_edge_pct,mr_revalidation_min_spread_pct,mr_max_hold_seconds,mr_max_bbo_spread_bps,mr_take_profit_fraction,mr_sigma_stop,mr_min_stop_distance_pct,mr_max_baseline_mean_pct,
trade_count,total_net_pnl_usdt,total_gross_pnl_usdt,total_fees_usdt,total_slippage_usdt,
winrate,profit_factor,sharpe_like,max_drawdown_usdt,
mean_reversion_count,mean_reversion_wr,mean_reversion_avg_pnl,
timeout_count,timeout_wr,timeout_avg_pnl,
stop_loss_count,stop_loss_wr,stop_loss_avg_pnl,
stale_quote_count,timeout_loss_count,
avg_hold_seconds,score
```

Кроме этого — ещё два файла **по лучшей конфигурации**:
- `backtest_best_by_symbol.csv` — breakdown trades по `symbol` (cnt, wr, total_pnl)
- `backtest_best_by_pair.csv` — breakdown по `long_exchange->short_exchange`
- `backtest_best_by_hour.csv` — breakdown по часам UTC

Эти три CSV нужно создавать только для лучшего по `score` прогона.

### CLI

```bash
python scripts/backtest.py \
    --db data/spread_arb_proj.sqlite3 \
    --since 2026-04-01 \
    --until 2026-05-25 \
    --symbols all \
    --output backtest_results.csv
```

Параметры:
- `--db` — путь к SQLite
- `--since` / `--until` — date range (ISO 8601 даты или datetime)
- `--symbols` — `all` (все символы из snapshot'ов) или `env` (из текущего .env) или явный список `BTCUSDT,ETHUSDT,...`
- `--output` — путь к CSV

При `--symbols all` динамически собираем уникальные символы из snapshot'ов в указанном диапазоне.

## Часть 3: `scripts/backtest_report.py` — парсер CSV

CLI:

```bash
python scripts/backtest_report.py backtest_results.csv
python scripts/backtest_report.py backtest_results.csv --top 5
python scripts/backtest_report.py backtest_results.csv --best-only
```

Выводит в stdout (форматированно — как `analyze_live.py`):

### Раздел 1: Top N по разным метрикам

```
═══════════════════════════════════════════════
       BACKTEST REPORT
═══════════════════════════════════════════════

TOP 5 BY SCORE (composite PnL × WR)
───────────────────────────────────────────────
  #1 score=+12.45  pnl=+15.20  wr=0.62  trades=234   {sigma_entry:2.5, min_net_edge:0.30, ...}
  #2 score=+10.18  pnl=+13.50  wr=0.55  trades=189   {sigma_entry:3.0, min_net_edge:0.30, ...}
  ...

TOP 5 BY TOTAL PNL
───────────────────────────────────────────────
  ...

TOP 5 BY WINRATE (min 20 trades)
───────────────────────────────────────────────
  ...

TOP 5 BY PROFIT FACTOR (min 20 trades)
───────────────────────────────────────────────
  ...
```

### Раздел 2: Per-parameter sensitivity (по результатам всех sweep'ов)

Группировать по `sweep_name`, показывать таблицу `param_value → score, pnl, wr, trades` чтобы видеть как параметр влияет:

```
SENSITIVITY: mr_sigma_entry
───────────────────────────────────────────────
  value=2.0   score=+8.5   pnl=+10.2   wr=0.45  trades=412
  value=2.5   score=+12.5  pnl=+15.2   wr=0.62  trades=234  ← BEST
  value=3.0   score=+10.2  pnl=+13.5   wr=0.55  trades=189
  value=3.5   score=+6.1   pnl=+8.9    wr=0.51  trades=98
  value=4.0   score=+2.3   pnl=+3.4    wr=0.48  trades=42
```

### Раздел 3: Best config detail

Для лучшего по score прогона показать полный конфиг и сводку:

```
BEST CONFIGURATION
───────────────────────────────────────────────
  mr_sigma_entry:                 2.5
  mr_min_net_edge_pct:            0.30
  mr_revalidation_min_spread_pct: 0.60
  ...

  Total trades:    234
  Net PnL:         +15.20 USDT
  Winrate:         62%
  Profit factor:   1.85
  Sharpe-like:     1.24
  Max drawdown:    -3.45 USDT

  By close reason:
    mean_reversion: 156 (66.7%)  wr=0.72  avg=+0.12
    timeout:         52 (22.2%)  wr=0.45  avg=-0.02
    stop_loss:       18 ( 7.7%)  wr=0.00  avg=-0.18
    stale_quote:      6 ( 2.6%)  wr=0.00  avg=-0.03
    timeout_loss:     2 ( 0.9%)  wr=0.00  avg=-0.08
```

### Раздел 4: Breakdown best (если файлы есть)

Если `backtest_best_by_symbol.csv`, `backtest_best_by_pair.csv`, `backtest_best_by_hour.csv` существуют рядом — подгружать и показывать топ-15 по каждому. Если нет — пропускать без ошибки.

## Acceptance criteria

1. `python -m compileall src/spread_arb/mean_reversion_engine.py scripts/backtest.py scripts/backtest_report.py tests/test_backtest_clock.py` проходит.
2. Все существующие тесты проходят (включая `test_mean_reversion_smart_exits.py`) — clock injection не сломал ничего.
3. Новые тесты `test_backtest_clock.py` (2 теста) проходят.
4. `scripts/backtest.py --help` показывает доступные опции.
5. `scripts/backtest.py --db <path> --since 2026-05-20 --until 2026-05-25 --symbols env --output /tmp/test.csv` выполняется без ошибок на маленькой выборке (5 дней).
6. CSV имеет все указанные колонки.
7. `scripts/backtest_report.py /tmp/test.csv` показывает сформатированный отчёт.
8. Никаких изменений в `execution.py`, `scanner.py`, `storage.py`, `paper_engine.py`, exchange clients, ws_feeds.

## После завершения

Прислать diff. Ожидаемый объём:
- `mean_reversion_engine.py`: ~10-15 строк изменений (clock param + 5 replacement'ов)
- `scripts/backtest.py`: ~400-600 строк нового кода
- `scripts/backtest_report.py`: ~150-250 строк нового кода
- `tests/test_backtest_clock.py`: ~40-60 строк

## Что НЕ делать

- Не пытаться симулировать `best_bid_size` / `best_ask_size` — оставить fake-large.
- Не делать walk-forward split в V1 — только полный прогон по диапазону.
- Не делать parallel execution через multiprocessing — sequential достаточно.
- Не делать HTML reports — только CSV + text.
- Не добавлять Optuna или другие optimization libraries.
- Не трогать `paper_engine.py` — его не используем в backtest, использовать только `MeanReversionEngine` через `live_mode=False`.

## Возможные подводные камни

1. **Asyncio.sleep в `_execute_after_delay`**: если `mr_revalidation_delay_sec > 0`, бэктест будет реально ждать. Жёстко переопределить в `0.0` для backtest. Уже учтено выше.

2. **Cooldown между сделками** (`mr_cooldown_sec`): использует `last_closed_by_symbol` с реальным datetime. В бэктесте с инжектированным clock это будет работать корректно (cooldown = X simulated seconds).

3. **Baseline preload**: в backtest preload не нужен — engine cold-starts на исторических данных, первые `mr_rolling_window` snapshots уйдут на warmup. Это OK.

4. **Память**: при долгом диапазоне (2 месяца) накопится много `PaperTradeRecord` в InMemoryStore. С ~50K snapshots в день × 60 дней = 3M snapshots. Trade count при разумных параметрах будет ~100-500 за весь прогон. Не проблема.

5. **CSV перезапись при retry**: append mode чтобы при padaнии не терять предыдущие прогоны. Header писать только если файл новый.
