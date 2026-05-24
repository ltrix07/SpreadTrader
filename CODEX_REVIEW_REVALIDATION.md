# Codex Task: Review re-validation delay changes for critical bugs

## Goal
Review recent changes to `src/spread_arb/config.py` and `src/spread_arb/mean_reversion_engine.py` for critical bugs, logic errors, race conditions, or unintended side effects. Do NOT fix anything — only report findings.

## What was changed and why

We added a **re-validation delay** to the MR engine entry flow. Previously the bot detected a spread signal and entered almost immediately (500ms simulated delay). Now it waits **10 seconds** and re-checks that the spread is still alive before entering. This filters out short-lived "flash" spreads that disappear before execution completes.

### Changes in `src/spread_arb/config.py`

Two new settings added after `mr_take_profit_fraction`:

```python
mr_revalidation_delay_sec: float = Field(default=10.0, ge=0)
mr_revalidation_min_spread_pct: float = Field(default=0.40, ge=0)
```

- `mr_revalidation_delay_sec` — how long to wait after signal before re-checking (seconds). Replaces the old `simulated_execution_delay_ms / 1000` as the sleep duration in `_execute_after_delay()`.
- `mr_revalidation_min_spread_pct` — absolute minimum raw spread that must still exist after the delay. Acts as a hard floor.

### Changes in `src/spread_arb/mean_reversion_engine.py`

Three modifications inside `_execute_after_delay()` method:

**1. Delay source changed (line ~659):**

Before:
```python
delay_sec = self.settings.simulated_execution_delay_ms / 1000.0
```

After:
```python
delay_sec = self.settings.mr_revalidation_delay_sec if self.settings.mr_revalidation_delay_sec > 0 else self.settings.simulated_execution_delay_ms / 1000.0
```

**2. Spread floor check added (lines ~709-717) — BEFORE the existing sigma threshold check:**

```python
reval_floor = self.settings.mr_revalidation_min_spread_pct
if reval_floor > 0 and current_spread_pct < reval_floor:
    self.log.info(
        "mr reval REJECT | %s %s->%s | spread=%.4f%% < floor=%.4f%% | signal was %.4f%% %.1fs ago",
        symbol, current.long_exchange.value, current.short_exchange.value,
        current_spread_pct, reval_floor, current.signal_spread_pct, delay_sec,
    )
    return
```

**3. Confirmation log added (lines ~732-736) — AFTER all checks pass, BEFORE execution:**

```python
self.log.info(
    "mr reval CONFIRMED | %s %s->%s | spread=%.4f%% (was %.4f%%) | survived %.1fs delay | net_edge=%.4f%%",
    symbol, current.long_exchange.value, current.short_exchange.value,
    current_spread_pct, current.signal_spread_pct, delay_sec, net_edge_pct,
)
```

## What to check

### Critical — must verify:

1. **Race condition: `pending_entries_by_symbol` cleanup.** During the 10s sleep, a NEW signal for the same symbol could arrive in `_evaluate_signal()`. Check line 613: `if symbol in self.pending_entries_by_symbol: return` — does this correctly prevent duplicate entries? What happens to the old asyncio task if a new PendingMrEntry overwrites it? Is the old task cancelled or does it become a zombie?

2. **`planned_at` field is stale.** Line 637 still uses `simulated_execution_delay_ms` for `planned_at` calculation, but the actual delay is now `mr_revalidation_delay_sec`. This field is stored in `PendingMrEntry` — check if `planned_at` is used anywhere downstream and whether this mismatch causes incorrect behavior.

3. **Pending entry NOT cleaned up on REJECT.** When the floor check rejects at line 717 (`return`), the `PendingMrEntry` stays in `self.pending_entries_by_symbol`. Check where/how pending entries are cleaned up after `_execute_after_delay` finishes (both success and rejection paths). If NOT cleaned up on reject → the symbol will be blocked from new signals forever (line 613 check).

4. **`rolling_mean` and `rolling_std` staleness.** The re-validation at line 718 uses `current.rolling_mean` and `current.rolling_std` from the SIGNAL time (10 seconds ago). During those 10 seconds, the baseline may have shifted significantly. Check if this is correct or if we should re-fetch the current baseline. This could cause false positives (stale low mean → inflated edge) or false negatives (stale high mean → rejected good signal).

5. **`delay_sec` variable scope.** `delay_sec` is used in the log messages at lines 713-715 and 733-735. Verify it's correctly scoped — it's defined at line 659 outside the try block, used inside. Should be fine but confirm.

6. **Interaction with `simulated_execution_delay_ms`.** The old delay (500ms) was meant to simulate network latency for paper trading. Now we replaced it with 10s for a different purpose (spread confirmation). In paper mode, do we still want 10s delay? Or should paper mode use the old 500ms? Currently both live and paper use the same delay.

### Important but not critical:

7. **Config validation.** No cross-field validation between `mr_revalidation_min_spread_pct` (0.40) and `mr_min_net_edge_pct` (0.10). The floor could theoretically be set lower than what net_edge requires, making it redundant. Not a bug, just note it.

8. **Log message format.** Verify `current.signal_spread_pct` is a valid attribute of `PendingMrEntry` (it should be — check the dataclass definition at line ~88).

## Files to read

- `src/spread_arb/config.py` — full file, check new fields
- `src/spread_arb/mean_reversion_engine.py` — focus on:
  - `PendingMrEntry` dataclass (line ~88)
  - `_evaluate_signal()` method (line ~530) — where pending entries are created
  - `_execute_after_delay()` method (line ~654) — where changes were made
  - Any cleanup of `pending_entries_by_symbol` (grep for `del self.pending_entries` or `.pop(`)

## Output format

Report as a numbered list:
1. Issue description
2. Severity: CRITICAL / WARNING / INFO
3. Location (file + line range)
4. Suggested fix (one-liner description, do NOT implement)

If no critical issues found, state that explicitly.
