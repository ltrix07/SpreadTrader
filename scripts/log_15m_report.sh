#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/log_15m_report.sh [LOG_FILE] [MINUTES]
# Examples:
#   scripts/log_15m_report.sh
#   scripts/log_15m_report.sh ../data/spread_proj.log 15

LOG_FILE="${1:-../data/spread_proj.log}"
MINUTES="${2:-15}"
OUT_FILE="/tmp/spread_${MINUTES}m_$$.log"

if [[ ! -f "$LOG_FILE" ]]; then
  echo "Log file not found: $LOG_FILE" >&2
  exit 1
fi

if ! [[ "$MINUTES" =~ ^[0-9]+$ ]] || [[ "$MINUTES" -le 0 ]]; then
  echo "MINUTES must be a positive integer (got: $MINUTES)" >&2
  exit 1
fi

# Prefer last bot run start marker, fallback to first timestamped line.
START_LINE="$(grep -n "effective config | exchanges=" "$LOG_FILE" | tail -1 | cut -d: -f1 || true)"
if [[ -n "${START_LINE}" ]]; then
  START_TS="$(sed -n "${START_LINE}p" "$LOG_FILE" | cut -c1-19)"
else
  START_TS="$(awk 'length($0)>=19 && substr($0,5,1)=="-" && substr($0,8,1)=="-" {print substr($0,1,19); exit}' "$LOG_FILE")"
fi

if [[ -z "${START_TS:-}" ]]; then
  echo "Could not detect start timestamp in: $LOG_FILE" >&2
  exit 1
fi

START_EPOCH="$(date -d "$START_TS" '+%s')"
END_EPOCH="$((START_EPOCH + MINUTES * 60))"
END_TS="$(date -d "@$END_EPOCH" '+%Y-%m-%d %H:%M:%S')"

awk -v s="$START_TS" -v e="$END_TS" '
  length($0)>=19 {
    t=substr($0,1,19)
    if (t>=s && t<=e) print
  }
' "$LOG_FILE" > "$OUT_FILE"

TOTAL_LINES="$(wc -l < "$OUT_FILE" | tr -d ' ')"

echo "==============================================================="
echo "LOG WINDOW REPORT (${MINUTES}m)"
echo "==============================================================="
echo "Source file : $LOG_FILE"
echo "Start       : $START_TS"
echo "End         : $END_TS"
echo "Lines       : $TOTAL_LINES"
echo "Extracted   : $OUT_FILE"

echo
echo "[1] Levels summary"
awk -F' \\| ' 'NF>=2 {c[$2]++} END {for (k in c) printf "  %-8s %d\n", k, c[k]}' "$OUT_FILE" | sort

echo
echo "[2] WARN/ERROR/CRITICAL"
grep -E ' \| (WARNING|ERROR|CRITICAL) \| ' "$OUT_FILE" || echo "  (none)"

echo
echo "[3] Trading events"
grep -Ei 'ENTRY|EXIT|FILLED|Opening|Closing position|STOP|order|position' "$OUT_FILE" || echo "  (none)"

echo
echo "[4] Top noisy modules"
awk -F' \\| ' 'NF>=3 {c[$3]++} END {for (k in c) printf "%6d %s\n", c[k], k}' "$OUT_FILE" | sort -nr | head -20

echo
echo "[5] Activity per minute"
awk 'length($0)>=16 {m=substr($0,1,16); c[m]++} END {for (k in c) printf "%s  %d\n", k, c[k]}' "$OUT_FILE" | sort

echo "==============================================================="
