# Roadmap: Inter-Exchange Spread Scanner + Paper Execution Logger

## 1. Цель проекта

Построить минимальный, но честный инструмент для проверки жизнеспособности межбиржевой spread/arbitrage стратегии без реальной торговли.

Идея стратегии:

* На одной бирже цена монеты ниже.
* На другой бирже цена той же монеты выше.
* Бот фиксирует spread между best bid/ask.
* В paper-режиме симулирует одновременное открытие двух ног:

  * long на дешёвой бирже;
  * short на дорогой бирже.
* Затем симулирует закрытие позиции, когда spread схлопывается или срабатывают защитные условия.

Главная задача проекта — не заработать сразу, а собрать честную статистику:

* есть ли реальные спреды после комиссий;
* как часто они появляются;
* сколько живут;
* хватает ли ликвидности;
* какая чистая доходность после fees/slippage;
* насколько опасны задержки исполнения;
* масштабируется ли стратегия хотя бы до небольших объёмов.

---

## 2. Что НЕ делаем на первом этапе

На MVP-этапе не делаем:

* реальные ордера;
* подключение API-ключей для торговли;
* автоматическое перемещение средств между биржами;
* сложный UI;
* HFT-инфраструктуру;
* оптимизацию под миллисекунды;
* прогнозирование рынка;
* стратегию по свечам.

MVP должен быть scanner + paper-execution logger, а не полноценный trading bot.

---

## 3. Базовая архитектура

Проект можно писать на Python.

Рекомендуемый стек:

* Python 3.12/3.13
* asyncio
* aiohttp / websockets
* pydantic-settings
* SQLite для MVP
* SQLAlchemy async или простой aiosqlite
* pandas для анализа логов
* rich/loguru для логов в консоли

Папочная структура:

```text
spread-arb-scanner/
  README.md
  roadmap.md
  pyproject.toml
  .env.example
  src/
    spread_arb/
      __init__.py
      main.py
      config.py
      models.py
      exchanges/
        __init__.py
        base.py
        mexc.py
        bybit.py
        binance.py
      scanner.py
      paper_engine.py
      risk.py
      storage.py
      analytics.py
      logging_setup.py
  data/
    spread_arb.sqlite3
    exports/
  scripts/
    export_trades.py
    analyze_results.py
```

---

## 4. Биржи для первого теста

Начать лучше с 2–3 бирж.

Кандидаты:

* MEXC
* Bybit
* Binance
* Gate.io / OKX later

Для MVP достаточно:

* MEXC + Bybit
* затем добавить Binance

Важно: сначала использовать публичные market-data endpoints без API-ключей.

---

## 5. Какие рынки сканировать

На первом этапе лучше сканировать USDT perpetual futures, потому что стратегия long/short удобнее именно там.

Фильтры по символам:

* только пары `*USDT`;
* исключить слишком неликвидные пары;
* исключить пары с подозрительно огромными spread из-за мёртвого стакана;
* оставить пересечение символов между биржами.

Пример whitelist для старта:

```text
BTCUSDT
ETHUSDT
SOLUSDT
BNBUSDT
XRPUSDT
DOGEUSDT
TONUSDT
AVAXUSDT
LINKUSDT
ADAUSDT
```

После MVP добавить auto-discovery common symbols.

---

## 6. Какие данные собирать

Минимум:

* timestamp;
* exchange;
* symbol;
* best_bid_price;
* best_bid_size;
* best_ask_price;
* best_ask_size;
* receive_latency_ms;
* source_latency_ms, если биржа отдаёт timestamp;
* funding rate, если доступно;
* mark/index price, если доступно.

Для MVP хватит top-of-book bid/ask.

Для более честного теста позже добавить depth 5/20:

* bids: price + size;
* asks: price + size.

---

## 7. Формула spread

Для пары бирж A и B:

Сценарий 1:

* long на A по ask_A;
* short на B по bid_B.

Raw spread:

```text
spread_pct = (bid_B - ask_A) / ask_A * 100
```

Сценарий 2:

* long на B по ask_B;
* short на A по bid_A.

Raw spread:

```text
spread_pct = (bid_A - ask_B) / ask_B * 100
```

Net spread должен учитывать:

* taker fee на открытие long;
* taker fee на открытие short;
* taker fee на закрытие long;
* taker fee на закрытие short;
* slippage buffer;
* safety buffer.

Пример:

```text
estimated_roundtrip_cost_pct = open_fees + close_fees + slippage_buffer + safety_buffer
net_spread_pct = raw_spread_pct - estimated_roundtrip_cost_pct
```

---

## 8. Paper execution logic

### 8.1. Вход в paper-position

Открывать paper-position, если:

* symbol находится в whitelist;
* spread выше `ENTRY_NET_SPREAD_PCT`;
* обе биржи имеют свежие quotes;
* quote age меньше лимита;
* best bid/ask size достаточный для заданного paper volume;
* по этому symbol ещё нет открытой paper-position;
* cooldown после прошлой сделки истёк.

Пример env:

```env
ENTRY_NET_SPREAD_PCT=0.25
MIN_RAW_SPREAD_PCT=0.45
PAPER_NOTIONAL_USDT=100
MAX_QUOTE_AGE_MS=1500
SYMBOL_COOLDOWN_SEC=60
```

### 8.2. Симуляция исполнения

При открытии позиции записывать:

* planned entry timestamp;
* actual simulated execution timestamp;
* exchange_long;
* exchange_short;
* entry_long_price;
* entry_short_price;
* entry_spread_pct;
* estimated fees;
* estimated slippage;
* notional;
* liquidity snapshot.

Симуляция должна учитывать execution delay:

```env
SIMULATED_EXECUTION_DELAY_MS=500
```

То есть бот видит spread сейчас, но paper-fill должен происходить по quote через 500 ms, если такая quote доступна.

Если через delay spread исчез — это должно фиксироваться как missed/failed opportunity, а не как прибыльная сделка.

### 8.3. Выход из paper-position

Закрывать position, если выполняется одно из условий:

* spread схлопнулся до `EXIT_SPREAD_PCT`;
* позиция висит дольше `MAX_HOLD_SECONDS`;
* spread пошёл против позиции сильнее `STOP_SPREAD_PCT`;
* одна из бирж перестала отдавать свежие данные;
* funding event близко и держать позицию нежелательно.

Пример env:

```env
EXIT_SPREAD_PCT=0.05
STOP_SPREAD_PCT=0.80
MAX_HOLD_SECONDS=900
```

---

## 9. Что логировать

### 9.1. Opportunities

Каждый найденный spread выше raw threshold:

* id;
* timestamp;
* symbol;
* long_exchange;
* short_exchange;
* ask_long;
* bid_short;
* raw_spread_pct;
* estimated_net_spread_pct;
* available_long_size;
* available_short_size;
* quote_age_ms;
* reason;
* status: observed / opened / missed / rejected.

### 9.2. Paper trades

Для каждой paper-сделки:

* trade_id;
* symbol;
* long_exchange;
* short_exchange;
* notional_usdt;
* opened_at;
* closed_at;
* hold_seconds;
* entry_long_price;
* entry_short_price;
* exit_long_price;
* exit_short_price;
* entry_raw_spread_pct;
* exit_raw_spread_pct;
* gross_pnl_usdt;
* fees_usdt;
* slippage_usdt;
* funding_usdt;
* net_pnl_usdt;
* net_pnl_pct;
* close_reason;
* max_adverse_spread_pct;
* max_favorable_spread_pct.

### 9.3. Runtime health

* exchange connection status;
* websocket reconnects;
* quote update frequency;
* stale quotes;
* rejected opportunities;
* open paper positions;
* daily net PnL;
* average net spread;
* average hold time.

---

## 10. База данных MVP

SQLite tables:

```text
quotes
opportunities
paper_trades
runtime_events
```

### quotes

Можно хранить не все quotes, чтобы база не раздувалась. Для MVP:

* хранить только quotes, которые участвовали в opportunity;
* либо хранить snapshots раз в N секунд;
* полную историю писать позже в parquet/csv.

### opportunities

Основная таблица для анализа: сколько было потенциальных входов и почему они были/не были взяты.

### paper_trades

Главная таблица результата.

---

## 11. Метрики успеха MVP

После 1–2 недель paper-run нужно получить:

* total opportunities;
* opened paper trades;
* missed opportunities after execution delay;
* rejected due to liquidity;
* winrate;
* gross PnL;
* total fees;
* total slippage estimate;
* net PnL;
* average net PnL per trade;
* max drawdown;
* average hold time;
* best/worst symbols;
* best/worst exchange pairs;
* PnL sensitivity to fee/slippage assumptions.

MVP считается полезным, если он честно показывает один из вариантов:

1. стратегия не живёт после комиссий;
2. стратегия живёт только на отдельных монетах;
3. стратегия живёт только на маленьком объёме;
4. стратегия требует maker-исполнения/VIP fees;
5. стратегия имеет потенциал для live micro-test.

---

## 12. Конфиг `.env.example`

```env
APP_ENV=dev
LOG_LEVEL=INFO
DATABASE_URL=sqlite+aiosqlite:///data/spread_arb.sqlite3

EXCHANGES=mexc,bybit
MARKET_TYPE=perp
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT

PAPER_NOTIONAL_USDT=100
MAX_OPEN_POSITIONS=3
ONE_POSITION_PER_SYMBOL=true

MIN_RAW_SPREAD_PCT=0.45
ENTRY_NET_SPREAD_PCT=0.25
EXIT_SPREAD_PCT=0.05
STOP_SPREAD_PCT=0.80
MAX_HOLD_SECONDS=900
SYMBOL_COOLDOWN_SEC=60

MAX_QUOTE_AGE_MS=1500
SIMULATED_EXECUTION_DELAY_MS=500

TAKER_FEE_MEXC_PCT=0.05
TAKER_FEE_BYBIT_PCT=0.055
TAKER_FEE_BINANCE_PCT=0.05

SLIPPAGE_BUFFER_PCT=0.05
SAFETY_BUFFER_PCT=0.05

STORE_QUOTES=false
QUOTE_SNAPSHOT_INTERVAL_SEC=5
EXPORT_DIR=data/exports
```

---

## 13. Development phases

### Phase 0 — Project skeleton

Deliverables:

* pyproject.toml;
* src layout;
* config loader;
* logging setup;
* basic CLI entrypoint;
* SQLite init.

Command:

```bash
python -m spread_arb.main run
```

---

### Phase 1 — Public market data connectors

Deliverables:

* base exchange interface;
* MEXC connector;
* Bybit connector;
* normalized quote model;
* reconnect logic;
* stale quote detection.

Goal:

* stream or poll bid/ask for configured symbols;
* print normalized quotes in console.

---

### Phase 2 — Spread scanner

Deliverables:

* scanner compares every common symbol across exchange pairs;
* calculates both directions;
* applies raw/net filters;
* writes opportunities to DB;
* logs top spreads in console.

Goal:

* увидеть реальные спреды и частоту их появления.

---

### Phase 3 — Paper execution engine

Deliverables:

* open simulated long/short positions;
* execution delay simulation;
* close conditions;
* fee model;
* slippage model;
* paper_trades table.

Goal:

* получить первые честные paper-сделки.

---

### Phase 4 — Analytics scripts

Deliverables:

* export trades to CSV;
* daily summary;
* symbol summary;
* exchange-pair summary;
* fee/slippage sensitivity report.

Commands:

```bash
python scripts/export_trades.py
python scripts/analyze_results.py
```

---

### Phase 5 — Hardening

Deliverables:

* better websocket reconnects;
* rate-limit handling;
* health metrics;
* graceful shutdown;
* duplicate opportunity protection;
* symbol cooldowns;
* bad-symbol blacklist.

---

### Phase 6 — Optional dashboard

Only after paper data is useful.

Options:

* simple FastAPI + HTML page;
* Streamlit dashboard;
* Telegram daily report.

Do not build dashboard before the scanner proves useful.

---

## 14. Important implementation rules for Code LLM

* Do not implement real trading.
* Do not ask for private API keys.
* Use public market-data endpoints only.
* Keep architecture simple.
* Prefer clear logs over complex abstractions.
* All exchange-specific code must stay inside `exchanges/`.
* Normalize all quotes to one common model.
* Never calculate spread from last traded price; use bid/ask.
* Always subtract estimated roundtrip fees.
* Simulate execution delay before opening paper position.
* Record rejected/missed opportunities, not only successful paper trades.
* Make the project runnable locally on Windows PowerShell.
* Keep defaults conservative.

---

## 15. Definition of Done for MVP

MVP готов, когда:

* бот запускается одной командой;
* получает bid/ask минимум с двух бирж;
* находит spreads по общим символам;
* считает raw и net spread;
* логирует opportunities;
* открывает и закрывает paper positions;
* считает net PnL после fees/slippage;
* пишет результаты в SQLite;
* есть скрипт анализа результатов;
* можно оставить процесс на сервере на 7–14 дней и потом понять, есть ли edge.

---

## 16. First Code LLM prompt

```text
We are building a Python project called spread-arb-scanner.

Goal: create a scanner + paper-execution logger for inter-exchange spread trading between crypto perpetual futures markets. This is NOT a real trading bot. Do not implement real orders or private API keys. Use public market-data only.

Please implement Phase 0 and Phase 1 from roadmap.md.

Requirements:
- Python 3.12+ compatible.
- Use src layout.
- Add pyproject.toml.
- Add .env.example.
- Add config.py using pydantic-settings.
- Add logging setup.
- Add normalized models:
  - Quote
  - ExchangeName
  - Symbol
- Add exchange base interface in exchanges/base.py.
- Add at least two exchange connectors: MEXC and Bybit.
- Public market data only.
- For MVP, polling REST endpoints is acceptable if websocket implementation would take too long.
- Normalize best bid/ask into the same Quote model.
- Add main CLI command:
  python -m spread_arb.main run
- The command should continuously fetch configured symbols from configured exchanges and print normalized quote updates.
- Add graceful shutdown on Ctrl+C.
- Keep code simple and production-readable.
- Do not invent real trading functionality.

After implementation, provide:
- list of files changed;
- how to install;
- how to run;
- known limitations.
```
