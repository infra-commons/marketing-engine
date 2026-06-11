#!/usr/bin/env bash
# run-batch.sh — PRE-June-15-2026 headless batch.
#
# Before 15 Jun 2026, `claude -p` draws your SHARED subscription pool (same as
# interactive Claude Code), limited by the rolling ~5h window. There is no separate
# dollar credit to pace against — just run a capped batch and exit 75 on a usage-limit
# hit so the timer retries after the window resets.
#
# Keep MAX_ITERS MODEST (default 8): this shares the pool with interactive Claude Code.
# A runaway loop competes with your own sessions.
#
# MIGRATION (15 Jun 2026): switch the service ExecStart from run-batch.sh to run-once.sh,
# set BILLING_DAY=28 and MONTHLY_CREDIT_USD=20, and restore the 3×/day schedule.
# Same plan files, same loop.sh, same oracle — only the pacing layer changes.
#
# Required env:  PLAN_DIR
# Optional env:  MAX_ITERS (default 8), MODEL, PER_CALL_BUDGET_USD
set -euo pipefail
cd "$(dirname "$0")"

PLAN_DIR="${PLAN_DIR:?set PLAN_DIR, e.g. plans/marketing-agents}"

exec env \
    MAX_ITERS="${MAX_ITERS:-8}" \
    TOTAL_BUDGET_USD="${TOTAL_BUDGET_USD:-999}" \
    PER_CALL_BUDGET_USD="${PER_CALL_BUDGET_USD:-0.75}" \
    MODEL="${MODEL:-}" \
    ./loop.sh "${PLAN_DIR%/}"
