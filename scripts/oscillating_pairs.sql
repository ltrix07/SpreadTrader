-- Найти пары которые регулярно ходят туда-сюда (oscillating pairs).
-- Это лучший индикатор для mean-reversion стратегии — не "большой средний спред",
-- а "часто переходит из + в - и обратно".
--
-- Запуск:
--   sqlite3 src/data/spread_arb_proj.sqlite3 < scripts/oscillating_pairs.sql > oscillating_pairs.txt

.headers on
.mode column

.print
.print === 1. Top 30 pairs by oscillation count (sign flips in raw_spread_ab_pct) ===
.print Колонка zero_crossings: сколько раз спред пересёк ноль (тент туда-сюда).
.print Высокий count = хорошая mean-reverting пара. Низкий = структурный спред.
.print

WITH ranked AS (
    SELECT
        symbol,
        exchange_a || '<->' || exchange_b AS pair,
        raw_spread_ab_pct,
        LAG(raw_spread_ab_pct) OVER (
            PARTITION BY symbol, exchange_a, exchange_b
            ORDER BY timestamp
        ) AS prev_spread
    FROM spread_snapshots
    WHERE timestamp >= '2026-04-01' AND timestamp < '2026-05-26'
),
flips AS (
    SELECT
        symbol, pair,
        SUM(CASE WHEN (prev_spread < 0 AND raw_spread_ab_pct >= 0) OR
                      (prev_spread >= 0 AND raw_spread_ab_pct < 0) THEN 1 ELSE 0 END) AS zero_crossings,
        COUNT(*) AS n_samples
    FROM ranked
    WHERE prev_spread IS NOT NULL
    GROUP BY symbol, pair
)
SELECT symbol, pair, n_samples, zero_crossings,
       ROUND(100.0 * zero_crossings / n_samples, 2) AS flip_pct
FROM flips
WHERE n_samples > 500
ORDER BY zero_crossings DESC
LIMIT 30;

.print
.print === 2. Top 30 pairs by spread amplitude (max - min) ===
.print Большая амплитуда = много возможностей для входа на extremes.
.print

SELECT
    symbol,
    exchange_a || '<->' || exchange_b AS pair,
    COUNT(*) AS n_samples,
    ROUND(MIN(raw_spread_ab_pct), 3) AS min_ab,
    ROUND(MAX(raw_spread_ab_pct), 3) AS max_ab,
    ROUND(MAX(raw_spread_ab_pct) - MIN(raw_spread_ab_pct), 3) AS amplitude,
    ROUND(AVG(raw_spread_ab_pct), 3) AS mean_ab
FROM spread_snapshots
WHERE timestamp >= '2026-04-01' AND timestamp < '2026-05-26'
GROUP BY symbol, exchange_a, exchange_b
HAVING n_samples > 500 AND amplitude > 0.4
ORDER BY amplitude DESC
LIMIT 30;

.print
.print === 3. Best mean-reverting pairs (high oscillation + low |mean|) ===
.print Фильтр: amplitude > 0.5%, |mean| < 0.3% (не структурный),
.print          zero_crossings > 100 (часто пересекает ноль)
.print

WITH ranked AS (
    SELECT
        symbol,
        exchange_a || '<->' || exchange_b AS pair,
        raw_spread_ab_pct,
        LAG(raw_spread_ab_pct) OVER (
            PARTITION BY symbol, exchange_a, exchange_b
            ORDER BY timestamp
        ) AS prev_spread
    FROM spread_snapshots
    WHERE timestamp >= '2026-04-01' AND timestamp < '2026-05-26'
),
combined AS (
    SELECT
        symbol, pair,
        SUM(CASE WHEN (prev_spread < 0 AND raw_spread_ab_pct >= 0) OR
                      (prev_spread >= 0 AND raw_spread_ab_pct < 0) THEN 1 ELSE 0 END) AS zero_crossings,
        COUNT(*) AS n_samples,
        AVG(raw_spread_ab_pct) AS mean_ab,
        MAX(raw_spread_ab_pct) - MIN(raw_spread_ab_pct) AS amplitude
    FROM ranked
    WHERE prev_spread IS NOT NULL
    GROUP BY symbol, pair
)
SELECT
    symbol, pair, n_samples,
    zero_crossings,
    ROUND(amplitude, 3) AS amplitude,
    ROUND(mean_ab, 3) AS mean_ab,
    ROUND(100.0 * zero_crossings / n_samples, 2) AS flip_pct
FROM combined
WHERE amplitude > 0.5
  AND ABS(mean_ab) < 0.3
  AND zero_crossings > 100
ORDER BY (zero_crossings * amplitude) DESC
LIMIT 30;

.print
.print === 4. Sign-aware oscillation: pairs that go LARGE both directions ===
.print Пары которые НЕ просто прыгают вокруг 0, а имеют большие excursions в плюс И минус.
.print Это идеальные кандидаты для long+short по обе стороны.
.print

SELECT
    symbol,
    exchange_a || '<->' || exchange_b AS pair,
    COUNT(*) AS n_samples,
    SUM(CASE WHEN raw_spread_ab_pct > 0.3 THEN 1 ELSE 0 END) AS times_above_03,
    SUM(CASE WHEN raw_spread_ab_pct < -0.3 THEN 1 ELSE 0 END) AS times_below_neg03,
    ROUND(MIN(raw_spread_ab_pct), 3) AS min_ab,
    ROUND(MAX(raw_spread_ab_pct), 3) AS max_ab
FROM spread_snapshots
WHERE timestamp >= '2026-04-01' AND timestamp < '2026-05-26'
GROUP BY symbol, exchange_a, exchange_b
HAVING n_samples > 500
   AND times_above_03 > 20
   AND times_below_neg03 > 20
ORDER BY (times_above_03 + times_below_neg03) DESC
LIMIT 30;
