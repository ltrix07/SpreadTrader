# Full Code Audit — Independent Review

## Context (only for grounding, not as direction)

This is a cross-exchange perpetual futures arbitrage bot. It scans price spreads between 5 exchanges (Binance, Bybit, Bitget, Gate, OKX) on 12 perpetual symbols (ONDOUSDT, NEARUSDT, INJUSDT, PENDLEUSDT, TIAUSDT, SUIUSDT, ZENUSDT, ORDIUSDT, WIFUSDT, DYDXUSDT, CFXUSDT, SOLUSDT). When spread between two exchanges exceeds a threshold based on rolling baseline + sigma, the bot opens a long position on the cheaper exchange and a short on the expensive one (mean reversion expected). Position is closed when spread converges to baseline mean.

The bot has been actively developed and is currently running live with real money (~$58 balance, ~$11 notional per leg, 5x leverage). It has gone through multiple iterations of fixes for: smart timeout exits, liquidity filters, BBO walk in net edge calculation, winning counter, funding rate filter. Despite these, **for the last 24 hours there have been zero trades** — neither in profit nor in loss. The bot writes spread snapshots to the SQLite database normally, but no `mr signal` events are being logged after restart, and no baselines reach `is_ready` state.

This audit is meant to be **independent and unbiased**. Don't assume what the problem is. Don't try to fit findings into existing hypotheses. Look at the code as a fresh reviewer who hasn't been involved in the recent debugging.

## Scope

Audit **the entire `src/spread_arb/` package**:
- `mean_reversion_engine.py` — main strategy logic, signal generation, position management
- `scanner.py` — WS feeds orchestration, spread snapshot collection
- `execution.py` — order placement and management
- `exchanges/base.py` and all 6 exchange implementations
- `ws_feeds/*.py` — WebSocket connectors per exchange
- `paper_engine.py` — paper trading logic
- `config.py` — settings
- `storage.py` — SQLite operations
- `models.py`, `opportunity.py`, `symbol_rotator.py`, `logging_setup.py`, `main.py`

Also briefly check `scripts/` — but focus mainly on `src/`.

## What to look for

You're a senior engineer doing a code review pass. Look for problems in **any** of these categories:

1. **Logic bugs.** Off-by-one errors, wrong conditionals, sign errors, incorrect formulas. Trace key calculations end-to-end (e.g., net_edge, PnL, sigma) and verify against the comments/intent.

2. **Concurrency issues.** `asyncio` task lifecycle, race conditions, missing locks, unhandled cancellations. Many bugs in this codebase involve asyncio — pending tasks, fire-and-forget tasks, tasks that silently swallow exceptions.

3. **Silent failures.** Exception handlers that catch too broadly and log but continue with broken state. `try/except` blocks where the recovery path is suspicious. Any place where an error could prevent state updates without anyone noticing.

4. **State drift between paper mode and live mode.** Differences in how fills, fees, prices are computed. Places where `live_mode` branches diverge in subtle ways.

5. **Order/state mismatches.** Cases where a request was sent but local state assumes it succeeded (or vice versa). Position tracking inconsistencies. Orders placed but not tracked. Tracked orders that don't exist on exchange.

6. **Stale data hazards.** Quote freshness, baseline staleness, cache TTL correctness. Places where the engine could act on stale or inconsistent data.

7. **Configuration ambiguities.** Settings that interact poorly (e.g., default values that conflict), undocumented combinations, places where `.env` value is needed but engine has a default fallback that silently disagrees.

8. **Exchange-specific edge cases.** Each exchange has quirks (different symbol formats, fee tiers, position modes, leverage settings). Cases where the abstraction leaks. Cases where one exchange has a feature/behavior others don't.

9. **Resource leaks.** WS connections not closed properly, file handles, DB connections, asyncio tasks not awaited.

10. **Code smells worth flagging.** Hardcoded values that should be config. Duplicated logic. Functions doing too much. Unused parameters. Dead code paths.

**Don't** look for stylistic issues, formatting, naming conventions, or documentation gaps. Only substantive issues that could affect correctness, performance, or reliability.

## What NOT to do

- Don't fix anything. Just identify and report.
- Don't propose redesigns or new features.
- Don't repeat issues that are clearly documented as known TODOs (e.g., MEXC WebSocket integration).
- Don't focus only on the recent changes (smart_exits, funding_filter). The whole codebase is fair game.
- Don't assume the current zero-trades situation is necessarily due to a bug — but if you find something that could explain it, flag it.

## Output format

Produce a markdown file `CODEX_AUDIT_REPORT.md` at repo root with this structure:

```markdown
# Code Audit Report — <date>

## Summary
<2-3 sentences high-level: how many issues by severity, general code health impression>

## Critical
<issues that can cause data loss, money loss, or production failures>
For each:
- **Title** — one-line description
- **Location** — file:line(s)
- **What** — what's wrong
- **Why it matters** — concrete failure mode
- **Suggested action** — one-line fix direction (don't implement)

## High
<issues that could cause incorrect behavior under realistic conditions>
Same format.

## Medium
<issues likely to cause problems but only in edge cases>
Same format.

## Low
<code smells, minor inefficiencies, latent bugs unlikely to trigger>
Same format.

## Observations (not bugs)
<things worth flagging but not bugs — e.g., "this calculation assumes X which isn't documented anywhere">

## Files audited
<list of files you actually looked at>

## Files skipped
<files you skipped and why>
```

Each issue should be **independently actionable** — a developer reading just that one entry should understand what's wrong without reading the full report.

## Acceptance criteria

1. Report covers all major files in `src/spread_arb/`.
2. Each finding has a concrete file:line reference.
3. Findings are categorized by severity (Critical / High / Medium / Low).
4. No findings are speculative ("might be a bug, not sure"). Each is either a confirmed issue or moved to Observations.
5. No fixes are implemented — only diagnosis.
6. Report doesn't reference solutions from previous Codex prompts or assume any direction.

## Don't bias yourself

This is important. Recent work has been on:
- Smart timeout exits + liquidity filters
- BBO walk in net_edge
- Funding rate filter
- Backtester (with known V1 limitations)

The user wants a **fresh pair of eyes**. Don't validate or invalidate the recent work specifically — review it like any other code. If recent changes introduced bugs, find them. If they're fine, don't comment on them.

Similarly, don't assume the current zero-trades situation must have a code cause. It could be market conditions + correct config. But if you find a code reason that fits, name it explicitly.
