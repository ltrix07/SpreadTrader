# Smart timeout exits + liquidity check on entry

## Контекст

После анализа реальных live трейдов (см. `winrate_diag.txt`) выявлены три структурные проблемы, которые .env-правки не решают:

1. **Timeout exits ВСЕ убыточные** (7 трейдов, 0% winrate, total -$0.30). При delta спреда ~0.56% gross PnL отрицательный (-$0.015). Причина: timeout закрывает позицию по текущим рыночным ценам без проверки PnL. mean_reversion exit имеет guard `est_pnl > 0`, у timeout его нет.
2. **Один stop_loss трейд унёс треть всех потерь** (TACUSDT 22 мая: -$0.257 за 5 секунд от entry). Это явно low-liquidity символ где fills были катастрофическими. Текущий entry filter проверяет только `best_ask_size × best_ask_price >= notional` — depth дальше top-of-book не смотрим.
3. **Фильтр `mr_min_net_edge_pct` использует roundtrip_cost = 2×fees + slip_buffer + safety, но НЕ учитывает фактический BBO walk** на двух биржах. С `mr_max_bbo_spread_bps=15` это до 30 bps скрытой стоимости которую фильтр не видит. Объясняет почему сделки с `net_edge ≥ 0.10%` оказываются убыточными.

Все три правки локализованы в `src/spread_arb/mean_reversion_engine.py`. Изменения изолированы, тестируются отдельно. **Не трогать execution.py, scanner.py, storage.py, config.py для фич — только config.py для новых настроек.**

## Файлы для правки

- `src/spread_arb/mean_reversion_engine.py` — основная логика
- `src/spread_arb/config.py` — новые настройки
- `tests/test_mean_reversion_shutdown_recovery.py` — могут понадобиться правки если ломаются тесты
- Возможно потребуется новый тест: `tests/test_mean_reversion_smart_exits.py`

## Изменение №1: Symmetric PnL guard для timeout

### Что сейчас (mean_reversion_engine.py:574-616)

```python
elif hold_seconds >= self.settings.mr_max_hold_seconds:
    close_reason = "timeout"
elif age_long_ms > exit_max_age_ms or age_short_ms > exit_max_age_ms:
    close_reason = "stale_quote"
elif current_spread_pct <= position.take_profit_target:
    # ... считает est_pnl, держит если < 0 ...
```

Mean reversion exit имеет guard «не закрываться если est_pnl < 0», timeout — нет. Timeout закрывает always, в результате 100% timeout exits с убытком.

### Что нужно сделать

В `check_exits()`, в блоке timeout добавить **симметричный PnL guard**, аналогичный тому что есть для mean_reversion. Логика:

- При hit `hold_seconds >= mr_max_hold_seconds`:
  - Посчитать `est_pnl` тем же способом что для mean_reversion (использовать `calculate_pnl` с актуальными bid/ask ценами, фактическими entry fees, ожидаемыми exit fees + slippage)
  - **Если `est_pnl >= mr_timeout_min_pnl_usdt` (новая настройка, default 0.0)** — закрывать по причине "timeout"
  - **Если `est_pnl < threshold` И `hold_seconds < mr_timeout_max_hold_seconds`** (новая настройка, default = `2 × mr_max_hold_seconds`) — НЕ закрывать, продолжать ждать
  - **Если `est_pnl < threshold` И `hold_seconds >= mr_timeout_max_hold_seconds`** — закрывать всё равно по причине "timeout_loss" (новый close_reason для аналитики)

Иначе говоря, даём позиции «второй шанс» если она в минусе при первичном timeout, но только до hard ceiling, чтобы не висеть бесконечно.

Пример псевдокода:
```python
elif hold_seconds >= self.settings.mr_max_hold_seconds:
    est_exit_long = float(long_quote.best_bid_price)
    est_exit_short = float(short_quote.best_ask_price)
    exit_fees = _one_side_fees_usdt(notional_usdt=position.notional_usdt, ...)
    exit_slippage = _one_side_slippage_usdt(notional_usdt=position.notional_usdt, ...)
    est_pnl = calculate_pnl(
        notional_usdt=position.notional_usdt,
        entry_long_price=position.entry_long_price,
        entry_short_price=position.entry_short_price,
        exit_long_price=est_exit_long,
        exit_short_price=est_exit_short,
        entry_fees_usdt=position.estimated_entry_fees_usdt,
        exit_fees_usdt=exit_fees,
        entry_slippage_usdt=position.estimated_entry_slippage_usdt,
        exit_slippage_usdt=exit_slippage,
    )

    if est_pnl.net_pnl_usdt >= self.settings.mr_timeout_min_pnl_usdt:
        close_reason = "timeout"
    elif hold_seconds >= self.settings.mr_timeout_max_hold_seconds:
        close_reason = "timeout_loss"
        self.log.warning(
            "mr timeout HARD | %s | est_pnl=%+.4f below threshold but max hold reached - forcing close",
            symbol, est_pnl.net_pnl_usdt,
        )
    else:
        self.log.debug(
            "mr timeout SOFT | %s | est_pnl=%+.4f below threshold, extending hold (%.0fs/%.0fs)",
            symbol, est_pnl.net_pnl_usdt, hold_seconds, self.settings.mr_timeout_max_hold_seconds,
        )
        # close_reason остаётся None, не закрываем
```

**Важно**: эта проверка должна идти ПОСЛЕ stale_quote check (чтобы не считать PnL по stale ценам).

## Изменение №2: Liquidity depth check на входе

### Что сейчас (mean_reversion_engine.py:782-788, и в _execute_after_delay около 855-858)

```python
long_capacity = float(long_quote.best_ask_price * long_quote.best_ask_size)
short_capacity = float(short_quote.best_bid_price * short_quote.best_bid_size)
required_signal_notional = self.settings.mr_notional_usdt
if long_capacity < required_signal_notional or short_capacity < required_signal_notional:
    return
```

Это проверяет только top-of-book size. Если в top один уровень с нужным размером, но за ним стакан тонкий — fills будут плохие (см. TACUSDT).

### Что нужно сделать

Нам недоступна полная глубина стакана из Quote (только BBO). Поэтому решаем через **косвенный сигнал тонкого рынка**: широкий BBO + мелкий top size.

Добавить в `_evaluate_signal()` после существующих фильтров, до `mr_min_net_edge_pct` check:

1. Считать **safety capacity** = `min(top_size_long × ask_long, top_size_short × bid_short)` ≥ `mr_min_top_capacity_multiplier × notional` (новая настройка, default 3.0). Идея: если на top размер всего notional, нам впритык; если в 3× больше — есть запас.
2. Дополнительно — если bbo_bps на любой из бирж выше **половины** `mr_max_bbo_spread_bps`, требовать **более высокий** capacity multiplier (например `mr_min_top_capacity_multiplier × 2`). Логика: широкий BBO + малый top = тонкий стакан.

Псевдокод:
```python
top_capacity_long = float(long_quote.best_ask_price * long_quote.best_ask_size)
top_capacity_short = float(short_quote.best_bid_price * short_quote.best_bid_size)
min_top_capacity = min(top_capacity_long, top_capacity_short)

multiplier = self.settings.mr_min_top_capacity_multiplier
# Уплотняем требование если BBO широкий
if max(long_bbo_bps, short_bbo_bps) > (max_bbo_bps / 2):
    multiplier *= 2

if min_top_capacity < multiplier * required_signal_notional:
    self.log.debug(
        "mr filter LIQUIDITY | %s | top_capacity=$%.0f < %.1fx notional ($%.0f required) | bbo=%.1f/%.1f bps",
        symbol, min_top_capacity, multiplier, multiplier * required_signal_notional,
        long_bbo_bps, short_bbo_bps,
    )
    return
```

Поместить ЭТУ проверку до текущего `if long_capacity < required_signal_notional or short_capacity < required_signal_notional` (можно полностью заменить — она более строгая).

## Изменение №3: BBO-aware roundtrip cost для net_edge filter

### Что сейчас (mean_reversion_engine.py:736-743)

```python
roundtrip_cost_pct = _roundtrip_cost_pct(
    fee_long_pct=self._get_fee_pct(long_exchange),
    fee_short_pct=self._get_fee_pct(short_exchange),
    slippage_buffer_pct=self.settings.slippage_buffer_pct,
    safety_buffer_pct=self.settings.safety_buffer_pct,
)
expected_edge_pct = spread_pct - mean
net_edge_pct = expected_edge_pct - roundtrip_cost_pct
```

`roundtrip_cost_pct` = 2×fee_long + 2×fee_short + slippage_buffer + safety = ~0.22% при дефолтных fees. **Не учитывает реальный BBO walk** на двух биржах.

### Что нужно сделать

Добавить в roundtrip_cost фактическую BBO ширину обеих бирж (на ВХОДЕ известна, на выходе предполагаем такую же — это разумное приближение, fluctuations усреднятся).

Создать новый helper:
```python
def _bbo_walk_pct(long_quote: Quote, short_quote: Quote) -> float:
    """Estimated round-trip BBO walk cost as a percentage.

    On entry we pay best_ask on long, receive best_bid on short.
    On exit we receive best_bid on long, pay best_ask on short.
    Net cost per leg = (ask - bid) / mid ≈ BBO width.
    """
    long_bbo_pct = float((long_quote.best_ask_price - long_quote.best_bid_price) / long_quote.best_bid_price) * 100.0
    short_bbo_pct = float((short_quote.best_ask_price - short_quote.best_bid_price) / short_quote.best_bid_price) * 100.0
    # Round trip = full BBO width on each exchange (one direction at entry, opposite at exit)
    return long_bbo_pct + short_bbo_pct
```

В `_evaluate_signal` и в `_execute_after_delay` (где есть второй net_edge check):
```python
roundtrip_cost_pct = _roundtrip_cost_pct(...)
bbo_walk_pct = _bbo_walk_pct(long_quote, short_quote)
expected_edge_pct = spread_pct - mean
net_edge_pct = expected_edge_pct - roundtrip_cost_pct - bbo_walk_pct
```

И в логе `mr signal` добавить `bbo_walk` для трассировки:
```python
self.log.info(
    "mr signal | %s %s->%s | spread=%+.4f%% | mean=%+.4f%% | std=%.4f%% | sigma=%.2f | net_edge=%+.4f%% | bbo_walk=%.4f%% | fresh=%.0f%%/%.0f%% | bbo=%.1f/%.1f",
    ...
    bbo_walk_pct,
    ...
)
```

То же самое в `_execute_after_delay`:
```python
self.log.info(
    "mr reval CONFIRMED | %s %s->%s | spread=%.4f%% (was %.4f%%) | survived %.1fs delay | net_edge=%.4f%% (bbo_walk=%.4f%%)",
    ...
)
```

## Изменение №4: Новые настройки в config.py

Добавить в `Settings` после блока `mr_*`:

```python
# Smart exits and liquidity
mr_timeout_min_pnl_usdt: float = Field(default=0.0)
mr_timeout_max_hold_seconds: int = Field(default=1800, ge=60)  # 2× mr_max_hold_seconds дефолта
mr_min_top_capacity_multiplier: float = Field(default=3.0, gt=0)
```

Дефолты:
- `mr_timeout_min_pnl_usdt=0.0` — закрываем по timeout только если хотя бы безубыточно.
- `mr_timeout_max_hold_seconds=1800` — максимум 30 минут общего удержания (2× от текущего 900). С новыми .env правками (300s) это будет 30 минут — не страшно.
- `mr_min_top_capacity_multiplier=3.0` — требуем 3× notional в top-of-book.

## Тесты

В `tests/test_mean_reversion_smart_exits.py` (новый файл):

1. **test_timeout_pnl_guard_holds_position_in_loss** — позиция в минусе на timeout → не закрывается, hold продлевается.
2. **test_timeout_pnl_guard_closes_on_breakeven** — позиция в плюсе на timeout → закрывается с `close_reason="timeout"`.
3. **test_timeout_hard_close_after_max_hold** — позиция в минусе, прошло `mr_timeout_max_hold_seconds` → закрывается с `close_reason="timeout_loss"`.
4. **test_liquidity_filter_rejects_thin_top_of_book** — top_size×price < 3×notional → сигнал не проходит.
5. **test_liquidity_filter_doubles_multiplier_on_wide_bbo** — широкий BBO + средний top → multiplier×2 применяется.
6. **test_bbo_walk_reduces_net_edge** — net_edge с учётом bbo_walk на типичном случае.

Старые тесты в `test_mean_reversion_shutdown_recovery.py` могут продолжать работать — изменения изолированы.

## Acceptance criteria

1. Все существующие тесты проходят (`pytest tests/`).
2. Новые тесты в `test_mean_reversion_smart_exits.py` проходят.
3. В логе `mr signal` теперь видна колонка `bbo_walk`.
4. В логе `mr timeout SOFT/HARD` события видны когда применяются.
5. Новый `close_reason="timeout_loss"` корректно записывается в `paper_trades.close_reason`.
6. Никаких изменений в execution.py, storage.py, scanner.py не делаем.

## Что НЕ делать

- Не менять PnL calculation (`paper_engine.calculate_pnl`) — он корректный.
- Не менять `roundtrip_cost_pct` сигнатуру/семантику в paper_engine.py — там своя логика.
- Не реализовывать early stop_loss с PnL guard — это отдельная задача, симметричный для timeout логично, но для stop_loss слишком рискованно (если спред бежит против — закрывать надо немедленно).
- Не добавлять глубокий стакан depth fetching (нет в Quote) — обходимся косвенным сигналом через BBO+top.

## После завершения

Прислать diff на ревью. Ожидаемая длина изменений: ~80-120 строк в `mean_reversion_engine.py`, ~5 строк в `config.py`, ~150-200 строк в новом test файле.
