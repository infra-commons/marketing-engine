#!/usr/bin/env bash
# run-once.sh — ONE paced batch (Bucket 2: monthly Agent SDK credit).
#
# USE FROM 15 JUN 2026 ONWARD. Before that date, use run-batch.sh instead.
#
# Spreads the monthly credit ($20 Pro) across the billing cycle so it doesn't
# detonate on day one. Fire a few times a day via systemd/cron. Computes today's
# slice of remaining credit, runs loop.sh capped to that slice, and records spend in
# a ledger that auto-resets each billing cycle. Exits 75 when paced-out or empty.
#
# Required env:  PLAN_DIR, BILLING_DAY
# Optional env:  MONTHLY_CREDIT_USD (default 20), MODEL, PER_CALL_BUDGET_USD, LEDGER
set -euo pipefail
cd "$(dirname "$0")"

PLAN_DIR="${PLAN_DIR:?set PLAN_DIR, e.g. plans/marketing-agents}"
PLAN_DIR="${PLAN_DIR%/}"
BILLING_DAY="${BILLING_DAY:?set BILLING_DAY=28 (your subscription renewal day)}"
MONTHLY_CREDIT_USD="${MONTHLY_CREDIT_USD:-20}"
LEDGER="${LEDGER:-.autoloop/spend-ledger}"

command -v jq   >/dev/null || { echo "jq required" >&2; exit 1; }
command -v date >/dev/null || { echo "GNU date required" >&2; exit 1; }

(( BILLING_DAY > 28 )) && BILLING_DAY=28
today="$(date +%Y-%m-%d)"; y="$(date +%Y)"; m="$(date +%m)"; d="$(date +%d)"
bd="$(printf '%02d' "$BILLING_DAY")"
if (( 10#$d >= BILLING_DAY )); then
    cycle_start="$(date -d "$y-$m-$bd" +%Y-%m-%d)"
else
    cycle_start="$(date -d "$y-$m-$bd -1 month" +%Y-%m-%d)"
fi
next_reset="$(date -d "$cycle_start +1 month" +%Y-%m-%d)"
days_left=$(( ( $(date -d "$next_reset" +%s) - $(date -d "$today" +%s) ) / 86400 ))
(( days_left < 1 )) && days_left=1

mkdir -p "$(dirname "$LEDGER")"
lc=""; ls="0"
[[ -f "$LEDGER" ]] && { lc="$(sed -n 1p "$LEDGER")"; ls="$(sed -n 2p "$LEDGER")"; }
[[ "$lc" != "$cycle_start" ]] && { lc="$cycle_start"; ls="0"; }

remaining="$(awk -v c="$MONTHLY_CREDIT_USD" -v s="$ls" 'BEGIN{r=c-s; if(r<0)r=0; printf "%.4f", r}')"
today_budget="$(awk -v r="$remaining" -v dl="$days_left" 'BEGIN{printf "%.4f", r/dl}')"

echo "cycle $cycle_start -> $next_reset | days_left=$days_left | spent=\$$ls | remaining=\$$remaining | today=\$$today_budget"

if awk -v b="$today_budget" 'BEGIN{exit !(b < 0.05)}'; then
    echo "Paced out / credit near-empty. Resuming after $next_reset." >&2
    printf '%s\n%s\n' "$lc" "$ls" > "$LEDGER"
    exit 75
fi

spend_file="$(mktemp)"; rc=0
SPEND_OUT="$spend_file" \
TOTAL_BUDGET_USD="$today_budget" \
PER_CALL_BUDGET_USD="${PER_CALL_BUDGET_USD:-0.50}" \
MODEL="${MODEL:-}" \
    ./loop.sh "$PLAN_DIR" || rc=$?

run_spent="$(cat "$spend_file" 2>/dev/null || echo 0)"; rm -f "$spend_file"
new_spent="$(awk -v s="$ls" -v r="$run_spent" 'BEGIN{printf "%.4f", s+r}')"
printf '%s\n%s\n' "$lc" "$new_spent" > "$LEDGER"
echo "run_spent=\$$run_spent | cycle_total=\$$new_spent / \$$MONTHLY_CREDIT_USD (resets $next_reset)"

exit "$rc"
