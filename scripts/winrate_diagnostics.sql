-- Winrate diagnostics. Запустить на сервере:
--   sqlite3 data/spread_arb_proj.sqlite3 < scripts/winrate_diagnostics.sql > winrate_diag.txt
-- Затем прислать winrate_diag.txt.

.headers on
.mode column

.print
.print === 1. Overall stats ===
SELECT
    COUNT(*)                                                     AS total_trades,
    SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END)            AS wins,
    ROUND(100.0 * SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(net_pnl_usdt), 3)                                  AS total_net_pnl,
    ROUND(AVG(net_pnl_usdt), 4)                                  AS avg_pnl,
    ROUND(AVG(gross_pnl_usdt), 4)                                AS avg_gross,
    ROUND(AVG(fees_usdt), 4)                                     AS avg_fees,
    ROUND(AVG(slippage_usdt), 4)                                 AS avg_slip
FROM paper_trades;

.print
.print === 2. By close_reason ===
SELECT
    close_reason,
    COUNT(*)                                                     AS cnt,
    SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END)            AS wins,
    ROUND(100.0 * SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(net_pnl_usdt), 3)                                  AS total_net,
    ROUND(AVG(net_pnl_usdt), 4)                                  AS avg_net,
    ROUND(AVG(gross_pnl_usdt), 4)                                AS avg_gross,
    ROUND(AVG(hold_seconds), 0)                                  AS avg_hold,
    ROUND(AVG(entry_raw_spread_pct), 3)                          AS avg_entry_spr,
    ROUND(AVG(exit_raw_spread_pct), 3)                           AS avg_exit_spr,
    ROUND(AVG(entry_raw_spread_pct - exit_raw_spread_pct), 3)    AS avg_delta
FROM paper_trades
GROUP BY close_reason
ORDER BY cnt DESC;

.print
.print === 3. Wins vs losses comparison ===
SELECT
    CASE WHEN net_pnl_usdt > 0 THEN 'win' ELSE 'loss' END        AS result,
    COUNT(*)                                                     AS cnt,
    ROUND(AVG(net_pnl_usdt), 4)                                  AS avg_pnl,
    ROUND(AVG(gross_pnl_usdt), 4)                                AS avg_gross,
    ROUND(AVG(fees_usdt), 4)                                     AS avg_fees,
    ROUND(AVG(entry_raw_spread_pct), 3)                          AS avg_entry_spr,
    ROUND(AVG(exit_raw_spread_pct), 3)                           AS avg_exit_spr,
    ROUND(AVG(max_adverse_spread_pct), 3)                        AS avg_max_adv,
    ROUND(AVG(max_favorable_spread_pct), 3)                      AS avg_max_fav,
    ROUND(AVG(hold_seconds), 0)                                  AS avg_hold,
    ROUND(AVG(notional_usdt), 1)                                 AS avg_notional
FROM paper_trades
GROUP BY result;

.print
.print === 4. By exchange pair (long->short) ===
SELECT
    long_exchange || '->' || short_exchange                      AS pair,
    COUNT(*)                                                     AS cnt,
    SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END)            AS wins,
    ROUND(100.0 * SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(net_pnl_usdt), 3)                                  AS total_net,
    ROUND(AVG(net_pnl_usdt), 4)                                  AS avg_net
FROM paper_trades
GROUP BY pair
ORDER BY total_net ASC;

.print
.print === 5. MEXC vs non-MEXC ===
SELECT
    CASE WHEN long_exchange='mexc' OR short_exchange='mexc' THEN 'mexc_involved' ELSE 'no_mexc' END AS bucket,
    COUNT(*) AS cnt,
    SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
    ROUND(100.0 * SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(net_pnl_usdt), 3) AS total_net
FROM paper_trades
GROUP BY bucket;

.print
.print === 6. By symbol (top 15 by trade count) ===
SELECT
    symbol,
    COUNT(*) AS cnt,
    SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
    ROUND(100.0 * SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(net_pnl_usdt), 3) AS total_net,
    ROUND(AVG(net_pnl_usdt), 4) AS avg_net
FROM paper_trades
GROUP BY symbol
ORDER BY cnt DESC
LIMIT 15;

.print
.print === 7. Last 30 trades full detail ===
SELECT
    substr(opened_at,6,11)                                       AS opened,
    symbol,
    long_exchange || '->' || short_exchange                      AS pair,
    ROUND(notional_usdt, 1)                                      AS notional,
    ROUND(hold_seconds, 0)                                       AS hold_s,
    ROUND(entry_raw_spread_pct, 3)                               AS entry_spr,
    ROUND(exit_raw_spread_pct, 3)                                AS exit_spr,
    ROUND(gross_pnl_usdt, 3)                                     AS gross,
    ROUND(fees_usdt, 3)                                          AS fees,
    ROUND(slippage_usdt, 4)                                      AS slip,
    ROUND(net_pnl_usdt, 3)                                       AS net,
    close_reason
FROM paper_trades
ORDER BY opened_at DESC
LIMIT 30;

.print
.print === 8. Distribution of net PnL ===
SELECT
    CASE
        WHEN net_pnl_usdt < -1.0  THEN 'a: <-1.00'
        WHEN net_pnl_usdt < -0.5  THEN 'b: -1.00..-0.50'
        WHEN net_pnl_usdt < -0.2  THEN 'c: -0.50..-0.20'
        WHEN net_pnl_usdt < -0.05 THEN 'd: -0.20..-0.05'
        WHEN net_pnl_usdt < 0     THEN 'e: -0.05..0'
        WHEN net_pnl_usdt < 0.05  THEN 'f: 0..0.05'
        WHEN net_pnl_usdt < 0.2   THEN 'g: 0.05..0.20'
        WHEN net_pnl_usdt < 0.5   THEN 'h: 0.20..0.50'
        ELSE 'i: >0.50'
    END AS bucket,
    COUNT(*) AS cnt
FROM paper_trades
GROUP BY bucket
ORDER BY bucket;

.print
.print === 9. Live-only (since the bot went live with current settings; adjust date as needed) ===
SELECT
    COUNT(*) AS cnt,
    SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
    ROUND(100.0 * SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr_pct,
    ROUND(SUM(net_pnl_usdt), 3) AS total_net
FROM paper_trades
WHERE opened_at >= '2026-05-20';  -- ПОДСТАВЬ свою дату начала live торговли

.print
.print === 10. Average gross PnL vs fees per trade (cost ratio) ===
SELECT
    close_reason,
    COUNT(*) AS cnt,
    ROUND(AVG(ABS(gross_pnl_usdt)), 4) AS avg_abs_gross,
    ROUND(AVG(fees_usdt), 4)           AS avg_fees,
    ROUND(AVG(slippage_usdt), 4)       AS avg_slip,
    ROUND(AVG(fees_usdt + slippage_usdt), 4) AS avg_cost,
    ROUND(AVG(fees_usdt + slippage_usdt) / NULLIF(AVG(ABS(gross_pnl_usdt)), 0), 2) AS cost_to_gross_ratio
FROM paper_trades
GROUP BY close_reason
ORDER BY cnt DESC;
