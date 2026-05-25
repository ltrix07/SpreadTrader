# SpreadTrader — Session Handoff Document

> Контекст для нового чата Claude. Прочитай этот файл целиком перед началом работы.

## Кто я

Volodymyr (skotarenko138@gmail.com). Я — владелец проекта. Claude — мой ассистент по архитектуре и аналитике. Может смотреть код, логи, делать мелкие правки в несколько строк. Большие модули пишет Codex — мы пишем ему промты под задачу в формате `CODEX_*.md` файлов в корне репо.

## Что это за проект

Криптовалютный бот для арбитража спредов на USDT perpetual futures. Работает на 5 биржах: Binance, Bybit, Bitget, Gate, OKX. Стратегия — mean reversion: когда спред между биржами отклоняется от скользящего среднего на N сигм, бот открывает позицию (long на дешёвой бирже, short на дорогой), ожидая что спред вернётся к среднему.

## Архитектура

### Ключевые файлы

- `src/spread_arb/config.py` — конфигурация через pydantic-settings, читает `.env`
- `src/spread_arb/scanner.py` — главный event loop: WS фиды, snapshot collection, координация
- `src/spread_arb/mean_reversion_engine.py` — ядро MR стратегии: baselines, signals, entry/exit
- `src/spread_arb/execution.py` — исполнение ордеров на биржах (spread entry/exit)
- `src/spread_arb/exchanges/*.py` — адаптеры для каждой биржи (REST + WS)
- `src/spread_arb/symbol_rotator.py` — динамическая ротация символов по расписанию
- `src/spread_arb/storage.py` — SQLite storage (paper_trades, spread_snapshots, balance_snapshots)
- `scripts/analyze_live.py` — dashboard для анализа live торговли
- `scripts/model_filters.py` — моделирование фильтров на lifecycle данных

### Поток данных

1. WS фиды получают котировки → `_on_quote()` → `check_exits()` (для открытых позиций)
2. Каждые 10 секунд: `_collect_spread_snapshots()` → `update_baselines()` → `_evaluate_signal()`
3. Если signal проходит все фильтры → `_execute_after_delay()` (10s re-validation) → `_open_position()`
4. Позиция мониторится → exit по mean_reversion / stop_loss / timeout / stale_quote / shutdown

### Фильтры entry (в порядке применения)

1. `mr_sigma_entry` (2.5) — спред > mean + sigma * std
2. `mr_max_baseline_mean_pct` (0.50) — отсечение структурных спредов (mean > 0.50%)
3. `mr_min_net_edge_pct` (0.10) — net_edge = spread - mean - roundtrip_cost > 0.10%
4. `mr_max_bbo_spread_bps` (15.0) — BBO ширина < 15 bps на обеих биржах
5. `mr_min_quote_freshness_pct` (80%) — минимум 80% свежих котировок в rolling window
6. **Re-validation delay** (10s) — ждём 10 секунд, повторно проверяем:
   - `mr_revalidation_min_spread_pct` (0.40) — спред всё ещё > 0.40%
   - net_edge всё ещё > 0.10% (с fresh baseline)
   - sigma всё ещё > sigma_entry

### Roundtrip cost formula

```
roundtrip = 2 * fee_long + 2 * fee_short + slippage_buffer + safety_buffer
```
При стандартных fee 0.05%: roundtrip = 0.22%

## Текущее состояние на сервере

### Сервер

- Host: `srv1411923` (root)
- Path: `~/trading-bots-test/SpreadTrader/src/`
- Python: `.venv` с pydantic, aiohttp, aiosqlite
- Запуск: `PYTHONUNBUFFERED=1 nohup python -m spread_arb.main run >> bot.log 2>&1 &`
- БД: `data/spread_arb_proj.sqlite3` (2.6 GB)
- Лог: `bot.log` (append mode)

### .env на сервере (текущие настройки)

```
DATABASE_URL=sqlite+aiosqlite:///data/spread_arb_proj.sqlite3
EXCHANGES=binance,bybit,bitget,gate,okx
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,DOTUSDT,ATOMUSDT,UNIUSDT,ARBUSDT,OPUSDT,SUIUSDT,SEIUSDT,INJUSDT,NEARUSDT,WIFUSDT,FETUSDT,PENDLEUSDT,JUPUSDT,ENAUSDT,ONDOUSDT,TIAUSDT,1000BONKUSDT,AAVEUSDT,STXUSDT,RUNEUSDT,DYDXUSDT,GRTUSDT,BLURUSDT,GALAUSDT,CFXUSDT,IMXUSDT,GMXUSDT,MASKUSDT,ZKUSDT,STRKUSDT,ACHUSDT,CELOUSDT,ZENUSDT,ORDIUSDT,JCTUSDT,FIDAUSDT,LABUSDT,MEUSDT
LIVE_TRADING=true
MIN_RAW_SPREAD_PCT=0.30
MR_SIGMA_ENTRY=2.5
MR_MIN_NET_EDGE_PCT=0.10
MR_REVALIDATION_DELAY_SEC=10.0
MR_REVALIDATION_MIN_SPREAD_PCT=0.40
MR_MAX_BASELINE_MEAN_PCT=0.50
MR_MAX_POSITIONS=1
MR_COOLDOWN_SEC=120
MR_ROLLING_WINDOW=360
MR_NOTIONAL_PCT=85.0
MR_COMPOUND_ENABLED=true
DYNAMIC_ROTATION_ENABLED=true
DEFAULT_LEVERAGE=3
```

### Баланс

- Стартовый: $62.48 (распределён по 5 биржам ~$12.5 каждая)
- Текущий (~25 мая): ~$58.93 (-$3.55, -5.7%)
- Записанных трейдов: 24, net PnL -$0.83
- Разница ($2.72) — незаписанные трейды от kill'ов без graceful shutdown (теперь исправлено)

## Что было сделано (хронологически)

### Инфраструктурные фиксы
1. Fix MEXC avg_price=0 в place_market_order
2. Zero-price protection в calculate_pnl
3. BBO width filter (mr_max_bbo_spread_bps)
4. Dynamic symbol rotation (Codex)
5. Spread lifecycle collector (Codex) — отдельный скрипт для сбора данных о жизненном цикле спредов

### Re-validation delay (ключевое улучшение)
- **Проблема:** бот входил в flash спреды, которые исчезали за секунды
- **Решение:** 10-секундная задержка после сигнала + повторная проверка spread > 0.40% и net_edge > 0.10%
- **Результат:** по модели ~36% сигналов выживают delay, avg spread после delay 0.57%

### Structural spread filter
- **Проблема:** MRVLUSDT имел persistent spread 1.28% — проходил все фильтры, но MR exit никогда не срабатывал (спред не converge)
- **Решение:** `mr_max_baseline_mean_pct=0.50` — автоматически reject пар где rolling_mean > 0.50%
- Структурные символы (MRVLUSDT, RAVEUSDT, OPGUSDT, CBRSUSDT) убраны из SYMBOLS

### Fast baseline warmup при ротации (Codex)
- **Проблема:** baseline warmup 360 samples × 10s = 60+ мин. При ротации 3×/день = 3+ часов простоя
- **Решение:** `preload_baselines_for_symbols()` — загрузка из spread_snapshots таблицы для конкретных символов
- Helper: `_load_baselines_from_rows()` — общая логика для startup и rotation preload

### Graceful shutdown + orphan recovery (Codex)
- **Проблема:** при kill бот оставлял открытые live позиции на биржах, записи в paper_trades не создавались
- **Решение:** 
  - `shutdown()` — закрывает все open positions с close_reason="shutdown" перед выходом
  - `recover_orphan_positions()` — при старте сканирует все exchanges × symbols, закрывает orphan позиции
- **Fix:** pending close tasks теперь дожидаются завершения (не cancel'ятся) чтобы избежать double-close
- **Fix:** Bybit и OKX адаптеры теперь передают `reduceOnly` при `close=True`

## Известные проблемы и TODO

### Активные проблемы

1. **SOXLUSDT TradFi agreement** — Bybit и Binance требуют подписать TradFi Perps agreement для торговли SOXL. Бот пытается открыть long leg, получает ошибку, short leg уже открылся → убыточное закрытие. Нужно: подписать agreement на обеих биржах ИЛИ добавить SOXLUSDT в exclude list.

2. **Gate `get_position()` шумит при orphan recovery** — возвращает 400 POSITION_NOT_FOUND когда позиции нет, адаптер логирует как ERROR. Не критично, но шумно. Нужно: обработать POSITION_NOT_FOUND как size=0.

3. **Orphan recovery не сканирует dynamic rotation символы** — `recover_orphan_positions()` итерирует только `settings.symbols`, динамически добавленные символы пропускаются. Реальный случай: RKLBUSDT orphan на Gate не был найден.

4. **Binance orphan close вернул filled=0** — NEARUSDT на Binance: `filled=0 @ 0.00`. Подозрительно, может позиция не закрылась или адаптер неправильно парсит ответ.

5. **Баланс в dashboard не совпадает с trade PnL** — разница из-за незаписанных трейдов от предыдущих kill'ов. С новым graceful shutdown не должно повторяться, но исторические данные загрязнены.

6. **БД 2.6 GB и растёт** — spread_snapshots записываются каждые 10 секунд для ~50 символов × 10 пар = ~400 rows/цикл. Задача #1 (cleanup automation) до сих пор pending.

### Pending задачи

- **#1** Database & log cleanup automation
- Улучшить dynamic rotation — сейчас использует один REST snapshot, ненадёжно. Идеально: использовать lifecycle данные для выбора символов
- Рассмотреть mr_max_positions=2 когда баланс достигнет $120+

## Ключевые метрики из lifecycle данных (12 часов, 94K events)

### Распределение подтверждённых сигналов по часам UTC

| UTC | Confirmed сигналов |
|-----|-------------------|
| 18:00 | 4 |
| 20:00 | **9** (пик) |
| 22:00 | 0 |
| остальные | 0 |

- Пик торговли: **20:00 UTC** (23:00 MSK) — закрытие US сессии
- Мёртвые часы: 22:00-06:00 UTC — почти нет profitable сигналов
- С mr_max_positions=1 бот ловит ~5 из 9 сигналов в пиковый час (slot blocking ~10 мин)

### Filter performance (Option D — текущие настройки)

- Raw signals: 36 за 12 часов
- Passed re-validation: 13 (36%)
- Rate: ~1.1/час (average), до 9/час в пик
- Avg spread after delay: 0.57%
- Avg net edge: 0.25%

## Правила работы

1. **Мелкие правки (до ~10 строк)** — делай сам через Edit
2. **Большие модули/фичи** — пиши промт для Codex в файл `CODEX_*.md`
3. **После Codex** — всегда делай контрольное ревью (ещё один Codex промт `CODEX_REVIEW_*.md`)
4. **Перед деплоем** — покажи команды для сервера (git pull, sed, restart)
5. **Логи** — `bot.log` на сервере, ищи `mr signal|mr reval|mr open|mr close` для MR engine, `grep -i critical` для ошибок

## Файлы Codex промтов (для справки)

- `CODEX_REVIEW_REVALIDATION.md` — ревью re-validation delay
- `CODEX_FAST_WARMUP.md` — быстрый прогрев baselines при ротации
- `CODEX_GRACEFUL_SHUTDOWN.md` — graceful shutdown + orphan recovery
- `CODEX_REVIEW_SHUTDOWN_RECOVERY.md` — ревью shutdown/recovery
