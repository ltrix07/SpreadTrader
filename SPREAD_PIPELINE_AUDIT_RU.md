# Аудит пайплайна spread-данных (2026-05-14)

## Статус данных в текущем workspace
- В `data/spread_arb.sqlite3` **нет** таблицы `spread_snapshots` (есть только `quotes`, `opportunities`, `paper_trades`, `runtime_events`).
- Поэтому пересчитать ваши метрики на выборке ~1.85M строк внутри этого workspace сейчас невозможно.
- Для воспроизводимого пересчета добавлен отдельный скрипт: `scripts/analyze_spreads_audit.py`.

## Что проверено по коду
- `src/spread_arb/scanner.py` (`_collect_spread_snapshots`, строки 421-509)
- `src/spread_arb/storage.py` (`SpreadSnapshotRecord` + `insert_spread_snapshots`, строки 159-174, 404-428)
- `scripts/analyze_spreads.py` (`compute_stats`, `print_report`, строки 59-277)

## Найденные проблемы

### CRITICAL

1. **Selection bias из-за `best_raw_spread_pct = max(spread_ab, spread_ba)`**
- Где: `src/spread_arb/scanner.py:506`, затем анализируется в `scripts/analyze_spreads.py:70`.
- Почему критично: в анализ попадает максимум из двух направлений на каждом снэпшоте, что систематически поднимает центр распределения и хвосты даже при нулевом edge. Это прямое завышение частоты "сигналов" и ожидаемого spread.
- Фикс: анализировать направления **раздельно** (`A->B` и `B->A`) или использовать `best` только как диагностическую метрику, но не как торговый сигнал.
- Реализация: в `scripts/analyze_spreads_audit.py` строятся directional-группы из `raw_spread_ab_pct` и `raw_spread_ba_pct`.

2. **Look-ahead bias: пороги считаются на всей выборке и применяются к той же выборке**
- Где: `scripts/analyze_spreads.py:79-91`.
- Почему критично: в реале на момент t известна только история до t, а не будущие данные. Текущая оценка завышает стабильность/качество сигналов.
- Фикс: rolling/expanding baseline (mean/std) только по прошлым наблюдениям.
- Реализация: в `scripts/analyze_spreads_audit.py` сигналы считаются через rolling-window без заглядывания вперед.

3. **Модель доходности завышает PnL: используется `entry spread - fee`, без корректной цели выхода**
- Где: `scripts/analyze_spreads.py:112-117`.
- Почему критично: метрика `mean_net_spread_at_2sigma` фактически предполагает, что весь entry spread монетизируется; не проверяется факт/скорость возврата к mean и не моделируется выходной спред. Это может завышать доходность кратно.
- Фикс: считать proxy edge как `(spread_entry - rolling_mean_at_entry) - roundtrip_cost`, отдельно считать hit-rate возврата к mean/таргету в заданный горизонт.
- Реализация: в `scripts/analyze_spreads_audit.py` добавлен `mean_expected_edge_to_mean_pct` и `mean_net_edge_pct`.

4. **Комиссии заданы константой 0.30% для всех пар**
- Где: `scripts/analyze_spreads.py:62, 248-249`.
- Почему критично: реальные пары имеют разный fee; это искажает ранжирование и долю "прибыльных" сигналов.
- Фикс: использовать `roundtrip_cost = 2*(fee_long + fee_short) + extra_cost` по конкретной паре.
- Реализация: в `scripts/analyze_spreads_audit.py` добавлена fee-map по биржам и парный roundtrip cost.

5. **`Est$/day` считает сигналы как независимые и суммируемые между коррелированными парами**
- Где: `scripts/analyze_spreads.py:207-210`, агрегирование в `231-236`.
- Почему критично: сигналы по одному символу и общему "лидирующему" exchange сильно пересекаются по времени; идёт двойной/тройной учёт возможности открыть сделку.
- Фикс: дедупликация хотя бы до уровня `(symbol, timestamp)` и далее портфельные лимиты (max positions, one-symbol-at-a-time).
- Реализация: в `scripts/analyze_spreads_audit.py` добавлен коэффициент инфляции `pair_hit_inflation_vs_symbol_hit`.

6. **Расхождение tradeability-фильтров: в боевой логике fresh=2s, в dataset до 30s**
- Где: scanner snapshot age `30_000ms` (`src/spread_arb/scanner.py:429, 474`) vs trading freshness `max_quote_age_ms` (в `QuoteScanner._evaluate_direction`).
- Почему критично: оценка "прибыльности" на 30s котировках может включать артефакты, которые недостижимы при реальном входе с 2s SLA.
- Фикс: в анализе считать отдельные отчёты по age-бакетам (`<=2s`, `2-5s`, `5-30s`) и ориентироваться на tradeable subset.
- Реализация: в `scripts/analyze_spreads_audit.py` добавлены метрики stale-доли (`stale_gt_2s_pct`).

### MODERATE

1. **Нормальность и ?-порог на не-нормальном распределении**
- Где: `scripts/analyze_spreads.py:87-91`.
- Комментарий: после `max()` распределение точно не симметрично нормальное; even без `max()` у крипто-микроструктуры тяжёлые хвосты.
- Фикс: percentile-based пороги (например, rolling p97.5/p99) или EVT/robust z-score.

2. **Нет проверки mean-reversion outcome (hit rate/timeout/adverse)**
- Где: `scripts/analyze_spreads.py` в целом.
- Комментарий: измеряется только факт превышения порога и длительность пребывания выше порога (`93-110`), но не исход сделки.
- Фикс: event-study: для каждого сигнала смотреть путь spread на горизонтах (30s/60s/300s), достижение target/stop/time-out.

3. **Отсутствует уникальный constraint на snapshot-ключ**
- Где: `src/spread_arb/storage.py:159-174`, `404-428`.
- Комментарий: явного бага дублей в коде нет, но при повторных прогонах/рестартах возможны дубликаты ключа `(timestamp,symbol,exchange_a,exchange_b)`.
- Фикс: добавить `UNIQUE` и `INSERT OR IGNORE` (или дедуп в анализе SQL-агрегацией).

### MINOR

1. **Population std (`/n`) вместо sample std (`/(n-1)`)**
- Где: `scripts/analyze_spreads.py:80`.
- Влияние: при n~3800 небольшое.

2. **Percentile индекс без интерполяции и потенциальный off-by-one**
- Где: `scripts/analyze_spreads.py:84-85`.
- Влияние: небольшое смещение p95/p99.

3. **Median для чётного n берёт верхний элемент**
- Где: `scripts/analyze_spreads.py:83`.
- Влияние: небольшое.

4. **`Time span` печатается из `stats[0].count`, а не из временного диапазона таблицы**
- Где: `scripts/analyze_spreads.py:157`.
- Влияние: косметическое/репортинговое.

## Проверки корректности scanner/storage

- Формула spread в `scanner` корректна для направления long A / short B:
  - `spread_ab = (bid_b - ask_a) / ask_a * 100` (`src/spread_arb/scanner.py:483`).
- Swap при нормализации порядка бирж выполнен консистентно:
  - меняются `ex_a/ex_b`, `spread_ab/spread_ba`, `bid_*`, `ask_*`, `age_*` (`488-493`).
- Вставка в SQLite соответствует порядку колонок:
  - schema `spread_snapshots` (`src/spread_arb/storage.py:159-174`) и `INSERT` (`410-415`) совпадают.
- `dict(self.latest_quotes)` в async-loop не создаёт race-condition с потоками (работа в одном event loop; копия нужна и корректна для безопасной итерации).

## Что сделано в коде
- Добавлен `scripts/analyze_spreads_audit.py` с:
  - проверкой целостности хранения;
  - проверкой совпадения формул spread с stored-полями;
  - оценкой смещения `max(ab,ba)`;
  - look-ahead-free rolling сигналами;
  - directional анализом (`ab` и `ba` отдельно);
  - парными комиссиями;
  - оценкой инфляции сигналов из-за overlap по символу.

## Как запустить пересчёт (когда будет БД со `spread_snapshots`)

```bash
python scripts/analyze_spreads_audit.py --db <path_to_db_with_spread_snapshots> --sigma 2.0 --rolling-window 360 --min-samples 400 --extra-cost 0.10
```

Если дадите путь к той самой БД с ~1,856,203 snapshots, пересчитаю и дам численное сравнение "до/после" по каждому CRITICAL смещению.
