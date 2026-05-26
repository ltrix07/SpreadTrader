# Funding rate awareness filter

## Контекст

После анализа стратегии успешного спред-трейдера и индустриальных рекомендаций (Hummingbot, академические работы) выяснилось что **funding rate** на perpetual futures — критически важный фактор которого нет в нашем боте.

**Проблема:** позиция держится 5-30 минут. За это время может пройти **funding payment** (Binance/OKX/Gate в 00:00/08:00/16:00 UTC, Bybit в 00:00/08:00/16:00, MEXC в 00:00/04:00/08:00/12:00/16:00/20:00 — 6 раз в день, чаще). Если мы держим long на бирже где funding **положительный** (longs платят) и/или short где funding **отрицательный** (shorts платят), мы **платим funding** во время удержания. На altах funding бывает 0.1-0.5% за payment — это может полностью съесть spread profit.

**Решение:** перед открытием позиции проверять upcoming funding payments на обеих биржах для ожидаемого hold period и **net funding cost**. Если net cost > порога — не открывать позицию или уменьшить notional.

## Файлы для изменения / создания

1. **`src/spread_arb/exchanges/base.py`** — добавить абстрактный метод `get_funding_info()`
2. **`src/spread_arb/exchanges/binance.py`** — реализация для Binance
3. **`src/spread_arb/exchanges/bybit.py`** — реализация для Bybit
4. **`src/spread_arb/exchanges/okx.py`** — реализация для OKX
5. **`src/spread_arb/exchanges/gate.py`** — реализация для Gate
6. **`src/spread_arb/exchanges/bitget.py`** — реализация для Bitget
7. **`src/spread_arb/exchanges/mexc.py`** — реализация для MEXC
8. **`src/spread_arb/models.py`** — добавить `FundingInfo` dataclass
9. **`src/spread_arb/mean_reversion_engine.py`** — funding cache + filter в `_execute_after_delay`
10. **`src/spread_arb/config.py`** — новые настройки
11. **`tests/test_funding_filter.py`** — новые тесты

## Часть 1: модель `FundingInfo`

В `src/spread_arb/models.py`:

```python
from datetime import datetime
from decimal import Decimal

@dataclass(frozen=True, slots=True)
class FundingInfo:
    """Current funding rate and next payment time for a symbol on an exchange."""
    exchange: ExchangeName
    symbol: str
    funding_rate: Decimal          # Текущая ставка (per cycle), может быть отрицательной
    next_funding_time: datetime    # UTC время следующего payment
    funding_interval_hours: int    # Обычно 4 или 8 часов
    fetched_at: datetime           # Когда мы получили эти данные (для cache TTL)
```

## Часть 2: метод `get_funding_info` в exchange clients

В `base.py`:

```python
async def get_funding_info(self, symbol: str) -> FundingInfo:
    """Return current funding rate and next funding time for a perp symbol."""
    raise NotImplementedError
```

Для каждого exchange реализовать. REST endpoints:

**Binance** (`api/v1/premiumIndex`):
- `GET https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}`
- Returns: `lastFundingRate`, `nextFundingTime` (ms), funding interval 8h по умолчанию.

**Bybit** (V5):
- `GET https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}`
- Returns: `fundingRate`, `nextFundingTime` (ms string), interval 8h.

**OKX**:
- `GET https://www.okx.com/api/v5/public/funding-rate?instId={symbol-SWAP}`
- Returns: `fundingRate`, `nextFundingTime` (ms string), interval 8h.

**Gate**:
- `GET https://api.gateio.ws/api/v4/futures/usdt/contracts/{contract}`
- Returns: `funding_rate`, `funding_next_apply` (unix sec), `funding_interval` sec.

**Bitget**:
- `GET https://api.bitget.com/api/v2/mix/market/funding-time?productType=usdt-futures&symbol={symbol}`
- Returns: `fundingTime` (ms string), `nextFundingTime` (ms string), `ratePeriod`.
- Funding rate отдельным endpoint: `GET /api/v2/mix/market/current-fund-rate?...`

**MEXC**:
- `GET https://contract.mexc.com/api/v1/contract/funding_rate/{symbol}`
- Returns: `fundingRate`, `nextSettleTime` (ms), `collectCycle` (hours).

Symbol mapping: каждый exchange использует свой формат (`BTCUSDT`, `BTC-USDT-SWAP`, `BTC_USDT`). Каждый client уже имеет логику преобразования через свои существующие хелперы — переиспользовать их.

Все вызовы — простые HTTP GET через `aiohttp.ClientSession`. Можно использовать паттерн из существующих методов (`get_balance`, `get_position`).

## Часть 3: funding cache в `MeanReversionEngine`

Funding rates меняются медленно (обновляются каждые 15-60 секунд биржей). Кешируем на 60 секунд.

В `MeanReversionEngine.__init__`:
```python
self._funding_cache: dict[tuple[ExchangeName, str], tuple[FundingInfo, datetime]] = {}
self._funding_cache_ttl_sec: float = 60.0
```

Helper:
```python
async def _get_funding_info(self, exchange: ExchangeName, symbol: str) -> FundingInfo | None:
    """Get funding info with 60-sec cache. Returns None if fetch fails."""
    cache_key = (exchange, symbol)
    now = self._clock()
    cached = self._funding_cache.get(cache_key)
    if cached is not None:
        info, fetched_at = cached
        if (now - fetched_at).total_seconds() < self._funding_cache_ttl_sec:
            return info
    if self.execution_service is None:
        return None
    client = self.execution_service.clients.get(exchange)
    if client is None:
        return None
    try:
        info = await client.get_funding_info(symbol)
    except Exception as exc:
        self.log.warning("funding fetch failed | %s %s | %s", exchange.value, symbol, exc)
        return None
    self._funding_cache[cache_key] = (info, now)
    return info
```

## Часть 4: funding filter в `_execute_after_delay`

ПОСЛЕ всех существующих фильтров (после liquidity check, перед reading spread/baseline), но ПЕРЕД самим открытием позиции:

```python
# Funding rate filter — не открывать если funding payments в hold window съедят профит
if self.live_mode and self.settings.mr_funding_filter_enabled:
    expected_hold_sec = self.settings.mr_max_hold_seconds
    expected_edge_pct = current_spread_pct - reval_mean  # potential gross gain
    max_funding_cost_pct = (
        expected_edge_pct * self.settings.mr_funding_max_cost_fraction
    )

    long_funding = await self._get_funding_info(current.long_exchange, symbol)
    short_funding = await self._get_funding_info(current.short_exchange, symbol)
    if long_funding is not None and short_funding is not None:
        now_iso = self._clock()
        expected_cost_pct = _estimate_net_funding_cost_pct(
            long_funding=long_funding,
            short_funding=short_funding,
            position_direction=current.direction,  # "ab" = long on A, short on B
            now=now_iso,
            hold_seconds=expected_hold_sec,
        )
        if expected_cost_pct > max_funding_cost_pct:
            self.log.info(
                "mr reval REJECT FUNDING | %s %s->%s | expected_cost=%.4f%% > max=%.4f%% (edge=%.4f%%) | "
                "long_rate=%.4f%% next=%s | short_rate=%.4f%% next=%s",
                symbol, current.long_exchange.value, current.short_exchange.value,
                expected_cost_pct, max_funding_cost_pct, expected_edge_pct,
                float(long_funding.funding_rate * 100),
                long_funding.next_funding_time.isoformat()[:19],
                float(short_funding.funding_rate * 100),
                short_funding.next_funding_time.isoformat()[:19],
            )
            return
```

Helper для расчёта:

```python
def _estimate_net_funding_cost_pct(
    *,
    long_funding: FundingInfo,
    short_funding: FundingInfo,
    position_direction: str,  # "ab" — long на a, short на b
    now: datetime,
    hold_seconds: int,
) -> float:
    """Estimate net funding cost (positive = we pay) over expected hold window.

    Convention:
      funding_rate > 0 → longs pay shorts
      funding_rate < 0 → shorts pay longs

    Our position is delta-neutral spread: long on one exchange + short on another.
    Net cost = (long_funding × n_payments_long) - (short_funding × n_payments_short)
      because:
        - Long position pays long_funding when positive (loses money)
        - Short position receives long_funding when positive on its exchange
        - We need to count how many payment cycles fall within [now, now+hold_seconds]
    """
    hold_end = now + timedelta(seconds=hold_seconds)

    def _payments_in_window(funding: FundingInfo) -> int:
        count = 0
        next_t = funding.next_funding_time
        interval = timedelta(hours=funding.funding_interval_hours)
        while next_t <= hold_end:
            if next_t > now:
                count += 1
            next_t += interval
        return count

    n_long = _payments_in_window(long_funding)
    n_short = _payments_in_window(short_funding)

    # Long position pays long_funding (if positive) or receives (if negative)
    long_cost = float(long_funding.funding_rate) * 100.0 * n_long  # %
    # Short position receives short_funding (if positive) or pays (if negative)
    short_gain = float(short_funding.funding_rate) * 100.0 * n_short  # %

    # Net cost = what long pays minus what short receives
    net_cost_pct = long_cost - short_gain
    return net_cost_pct
```

**Логика:** на типичных условиях, если pair имеет похожий funding на обеих биржах (например +0.01% обе), net cost ≈ 0 (long платит 0.01%, short получает 0.01% → нейтрально). Если funding rates сильно различаются (как у RON в скрине от знакомого пользователя — gate -0.86%, mexc -0.40%) — net cost может быть значимым.

## Часть 5: настройки в `config.py`

```python
# Funding rate awareness
mr_funding_filter_enabled: bool = True
mr_funding_max_cost_fraction: float = Field(default=0.30, ge=0.0, le=1.0)
# 0.30 означает: не открывать если funding cost > 30% от expected edge
# Например edge = 0.50%, max_cost = 0.50% × 0.30 = 0.15%. Если ожидаемый funding cost > 0.15%, skip.
```

## Часть 6: тесты в `tests/test_funding_filter.py`

```python
def test_estimate_net_funding_cost_no_payments_in_window():
    """If no funding payments fall within hold window, cost is 0."""
    # Long pays/receives based on next_funding_time being > hold_end
    ...

def test_estimate_net_funding_cost_one_payment_both_sides():
    """Net cost = long_rate - short_rate when one payment occurs in window."""
    # long rate +0.10%, short rate +0.05%, one cycle each → net = 0.10 - 0.05 = +0.05%
    ...

def test_estimate_net_funding_cost_short_receives_more():
    """When short funding rate is higher (more positive), it's net profit not cost."""
    # long +0.05%, short +0.15% → net cost = 0.05 - 0.15 = -0.10% (gain!)
    ...

def test_funding_filter_rejects_high_cost_position():
    """If expected cost > max_cost_fraction × edge, signal is rejected."""
    # Setup mock funding info, mock execution_service.clients, run _execute_after_delay
    ...

def test_funding_filter_disabled_does_not_check():
    """With mr_funding_filter_enabled=False, funding is not fetched."""
    ...
```

## Acceptance criteria

1. `python -m compileall src/spread_arb/...` проходит.
2. Все существующие тесты продолжают проходить (clock injection и smart_exits тесты не должны сломаться).
3. Новые тесты в `test_funding_filter.py` проходят (минимум 5).
4. Логи `mr reval REJECT FUNDING` появляются когда фильтр срабатывает.
5. Funding cache работает — повторные запросы к одной паре в течение 60 сек не делают новый HTTP request.
6. `mr_funding_filter_enabled=False` полностью отключает фильтр (никаких REST вызовов, никакой задержки).

## Что НЕ делать

- Не делать proactive funding fetching на background — только on-demand перед entry decision.
- Не реализовывать exit на funding flip — это отдельная фича (V2).
- Не учитывать historical funding rates — только current rate в моменте.
- Не трогать `scanner.py`, `execution.py`, `storage.py`, `paper_engine.py`.
- Для paper mode (live_mode=False) — funding filter skip-ить (нет execution_service.clients).

## Возможные подводные камни

1. **Race condition на funding payment timestamp**: если payment вот-вот произойдёт (через 30 секунд), а наш hold_seconds = 300 — payment попадёт в окно. Это OK, мы это учитываем.

2. **Timezone**: все biz используют UTC, но возвращают timestamp в ms. Парсить как UTC.

3. **Funding interval**: большинство 8h, MEXC 4h, некоторые редкие — 1h. Не хардкодить.

4. **API rate limits**: GET funding rate — обычно not rate-limited агрессивно, плюс у нас cache 60 сек. Не проблема.

5. **Backwards compatibility**: новый абстрактный метод `get_funding_info` — другие места кода не должны его вызывать. Не падать если он не реализован для какой-то биржи (логировать warning, возвращать None из `_get_funding_info`).
