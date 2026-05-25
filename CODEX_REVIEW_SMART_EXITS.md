# Review pass: Smart exits + liquidity check

## Контекст

Это контрольное ревью к изменениям из `CODEX_SMART_EXITS_LIQUIDITY.md`. Основная реализация корректна, но при ручном ревью найдены две существенные дыры и пара улучшений. Закрой их.

## Что НЕ трогать

- `_bbo_walk_pct` helper — формула правильная.
- Тесты которые уже есть в `tests/test_mean_reversion_smart_exits.py` — оставить как есть, только дописать новые.
- `_estimate_exit_pnl` helper — корректный.
- Порядок проверок в `check_exits` (stop_loss → stale_quote → timeout → TP target) — правильный.

## Правка №1 (КРИТИЧНО): liquidity filter в `_execute_after_delay`

### Проблема

В `_evaluate_signal` есть полный liquidity filter:
```python
liquidity_multiplier = self.settings.mr_min_top_capacity_multiplier
if max_bbo_bps > 0 and max(long_bbo_bps, short_bbo_bps) > (max_bbo_bps / 2.0):
    liquidity_multiplier *= 2.0
required_top_capacity = liquidity_multiplier * required_signal_notional
if min_top_capacity < required_top_capacity:
    ...
    return
```

А в `_execute_after_delay` (после re-validation delay) — остался старый minimal check:
```python
long_capacity = float(long_quote.best_ask_price * long_quote.best_ask_size)
short_capacity = float(short_quote.best_bid_price * short_quote.best_bid_size)
if long_capacity < notional or short_capacity < notional:
    return
```

Это создаёт regression: за 10 секунд delay стакан мог истончиться, и старая проверка `1× notional` пропустит ситуации которые сигнальная фаза бы отсекла. Особенно критично потому что в сигнальной фазе используется `mr_min_notional_usdt` (small floor), а в re-val фазе — фактический notional. То есть **именно тут** должна быть строгая проверка.

### Что сделать

В `_execute_after_delay`, ПОСЛЕ блока расчёта `notional` (через `calculate_notional` или `mr_notional_usdt`) и ДО `current_spread_pct` расчёта, **заменить** существующий capacity check на полный liquidity filter с использованием фактического `notional` и тех же правил multiplier что в `_evaluate_signal`. Нужно знать `long_bbo_bps` и `short_bbo_bps` для решения о doubling — посчитать их тут же.

Псевдокод:
```python
# Заменить старые строки:
#   long_capacity = float(long_quote.best_ask_price * long_quote.best_ask_size)
#   short_capacity = float(short_quote.best_bid_price * short_quote.best_bid_size)
#   if long_capacity < notional or short_capacity < notional:
#       return

long_top_capacity = float(long_quote.best_ask_price * long_quote.best_ask_size)
short_top_capacity = float(short_quote.best_bid_price * short_quote.best_bid_size)
min_top_capacity = min(long_top_capacity, short_top_capacity)

reval_long_bbo_bps = float(
    (long_quote.best_ask_price - long_quote.best_bid_price) / long_quote.best_bid_price
) * 10_000
reval_short_bbo_bps = float(
    (short_quote.best_ask_price - short_quote.best_bid_price) / short_quote.best_bid_price
) * 10_000
max_bbo_bps = self.settings.mr_max_bbo_spread_bps

liquidity_multiplier = self.settings.mr_min_top_capacity_multiplier
if max_bbo_bps > 0 and max(reval_long_bbo_bps, reval_short_bbo_bps) > (max_bbo_bps / 2.0):
    liquidity_multiplier *= 2.0
required_top_capacity = liquidity_multiplier * notional
if min_top_capacity < required_top_capacity:
    self.log.info(
        "mr reval REJECT LIQUIDITY | %s %s->%s | top_capacity=$%.0f < %.1fx notional ($%.0f required) | bbo=%.1f/%.1f bps",
        symbol,
        current.long_exchange.value,
        current.short_exchange.value,
        min_top_capacity,
        liquidity_multiplier,
        required_top_capacity,
        reval_long_bbo_bps,
        reval_short_bbo_bps,
    )
    return
```

**Важно**: лог уровень тут `.info`, а не `.debug` как в `_evaluate_signal`. Это потому что в re-val фазе rejects уже редкие и важные для отслеживания (сравни с другими reval REJECT логами в этом же методе).

## Правка №2 (КРИТИЧНО): `winning_trades_count` считать любой positive PnL

### Проблема

В `_close_position` (примерно строка 1207):
```python
if close_reason == "mean_reversion" and pnl.net_pnl_usdt > 0:
    self.winning_trades_count += 1
```

С появлением нового close_reason `"timeout"` (закрытие по PnL guard, при положительном est_pnl) эти сделки фактически прибыльные, но не считаются как win во внутреннем счётчике. Лог `mr summary` показывает заниженный winrate, не совпадающий с `analyze_live.py` который считает любой `net_pnl > 0` как win.

### Что сделать

Изменить условие:
```python
if pnl.net_pnl_usdt > 0:
    self.winning_trades_count += 1
```

Простая правка. Не нужно никаких дополнительных условий — это унификация с external dashboard.

## Правка №3 (улучшение): DRY в mean_reversion exit guard

### Проблема

В `check_exits` блок mean_reversion exit (строки ~600-609) дублирует логику которая теперь есть в `_estimate_exit_pnl` helper:

```python
elif current_spread_pct <= position.take_profit_target:
    est_exit_long = float(long_quote.best_bid_price)
    est_exit_short = float(short_quote.best_ask_price)
    exit_fees = _one_side_fees_usdt(...)
    exit_slippage = _one_side_slippage_usdt(...)
    est_pnl = calculate_pnl(...)
    if est_pnl.net_pnl_usdt > 0:
        close_reason = "mean_reversion"
    else:
        self.log.debug(...)
```

### Что сделать

Заменить на:
```python
elif current_spread_pct <= position.take_profit_target:
    est_pnl = self._estimate_exit_pnl(
        position=position,
        long_quote=long_quote,
        short_quote=short_quote,
    )
    if est_pnl.net_pnl_usdt > 0:
        close_reason = "mean_reversion"
    else:
        self.log.debug(
            "mr exit guard | %s | spread reached target but est_pnl=%+.2f - holding",
            symbol,
            est_pnl.net_pnl_usdt,
        )
```

Поведение не меняется — только убирается дублирование.

## Правка №4 (улучшение): Изменить default `mr_timeout_min_pnl_usdt`

### Проблема

Дефолт `mr_timeout_min_pnl_usdt = 0.0` означает «закрываем по timeout если хотя бы breakeven». На notional ~$10 любая микро-погрешность между `est_pnl` и фактическим fill (~$0.005 расхождение это уже 0.05% от notional) может сделать сделку убыточной по факту, хотя guard её пропустил.

### Что сделать

В `config.py` поднять default:
```python
mr_timeout_min_pnl_usdt: float = Field(default=0.005)
```

Это $0.005 на $10 notional = 0.05% буфера сверх breakeven. Минимальный, но различимый запас. Юзер может переопределить через .env при желании.

## Правка №5: дополнить тесты

### Новый тест: liquidity filter в re-val phase

Добавить в `tests/test_mean_reversion_smart_exits.py`:

```python
def test_revalidation_rejects_thin_liquidity() -> None:
    """В _execute_after_delay тоже должен применяться liquidity filter."""
    async def _run() -> None:
        engine = _build_engine(
            mr_notional_usdt=100.0,
            mr_min_top_capacity_multiplier=3.0,
            mr_max_bbo_spread_bps=15.0,
            mr_revalidation_delay_sec=0.05,
            mr_revalidation_min_spread_pct=0.0,
        )
        _seed_ready_baseline(
            engine,
            symbol="BTCUSDT",
            long_exchange=ExchangeName.BYBIT,
            short_exchange=ExchangeName.OKX,
            samples=[0.2, 0.3, 0.4],
        )
        now = datetime.now(UTC)
        # Толстый стакан на signal time (проходит фильтр signal phase)
        signal_long = _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="100.00", ask="100.03", bid_size="50", ask_size="50", received_at=now)
        signal_short = _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="101.00", ask="101.03", bid_size="50", ask_size="50", received_at=now)

        # Тонкий стакан на reval time (должен зарезать в _execute_after_delay)
        reval_long = _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="100.00", ask="100.03", bid_size="2", ask_size="2", received_at=now)
        reval_short = _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="101.00", ask="101.03", bid_size="2", ask_size="2", received_at=now)

        quotes_by_call: list[tuple[Quote, Quote]] = [(reval_long, reval_short)]
        def get_latest(exchange: ExchangeName, _symbol: str) -> Quote:
            long_q, short_q = quotes_by_call[0]
            return long_q if exchange == ExchangeName.BYBIT else short_q

        engine.get_latest_quote = get_latest

        engine._evaluate_signal(
            symbol="BTCUSDT",
            long_exchange=ExchangeName.BYBIT,
            short_exchange=ExchangeName.OKX,
            long_quote=signal_long,
            short_quote=signal_short,
            spread_pct=1.0,
            direction="ab",
            now=now,
        )
        # Сигнал должен был создать pending entry на толстом стакане
        assert "BTCUSDT" in engine.pending_entries_by_symbol

        pending = engine.pending_entries_by_symbol["BTCUSDT"]
        # Дождаться завершения reval task
        await asyncio.wait_for(pending.task, timeout=1.0)

        # Позиция НЕ должна была открыться из-за тонкого стакана в reval
        assert "BTCUSDT" not in engine.open_positions_by_symbol

    asyncio.run(_run())
```

### Новый тест: winning_trades_count учитывает timeout wins

```python
def test_winning_counter_includes_timeout_wins() -> None:
    """Любой profit инкрементирует winning_trades_count, не только mean_reversion."""
    async def _run() -> None:
        engine = _build_engine()
        position = _build_position(opened_seconds_ago=15)
        engine.open_positions_by_symbol[position.symbol] = position

        long_quote = _quote(exchange=ExchangeName.BYBIT, symbol="BTCUSDT", bid="101.0", ask="101.2", bid_size="10", ask_size="10")
        short_quote = _quote(exchange=ExchangeName.OKX, symbol="BTCUSDT", bid="99.4", ask="99.5", bid_size="10", ask_size="10")

        await engine._close_position(
            position=position,
            close_reason="timeout",
            long_quote=long_quote,
            short_quote=short_quote,
        )

        assert engine.winning_trades_count == 1
        assert engine.closed_trades_count == 1

    asyncio.run(_run())
```

(может потребоваться dummy execution_service или live_mode=False — выбирай как удобнее. Position в _build_position уже совместима с paper mode.)

## Acceptance criteria

1. Все существующие тесты в `tests/` продолжают проходить.
2. Два новых теста (`test_revalidation_rejects_thin_liquidity`, `test_winning_counter_includes_timeout_wins`) проходят.
3. В `_execute_after_delay` есть симметричный liquidity filter с правильным multiplier и BBO uplift.
4. В `_close_position` `winning_trades_count` инкрементируется на любой `net_pnl > 0`.
5. mean_reversion exit guard в `check_exits` использует `_estimate_exit_pnl` helper.
6. Default `mr_timeout_min_pnl_usdt = 0.005` в config.py.
7. `python -m compileall src/spread_arb/mean_reversion_engine.py src/spread_arb/config.py tests/test_mean_reversion_smart_exits.py` проходит.

## После завершения

Прислать diff. Файлы которые могут измениться:
- `src/spread_arb/mean_reversion_engine.py` (~30-50 строк изменений)
- `src/spread_arb/config.py` (1 строка)
- `tests/test_mean_reversion_smart_exits.py` (+2 теста, ~60 строк)
