#!/usr/bin/env bash
# queue-runner.sh — sequential autoloop queue processor
#
# Reads plans/queue (ordered list, one plan dir per line).
# Skips plans whose autoloop branch already has STATUS: DONE in progress.md.
# Runs the first pending plan via run-batch.sh (or $RUNNER if overridden).
#
# Safe to call repeatedly from a systemd timer — idempotent on an empty/all-done queue.
#
# Queue format (plans/queue):
#   plans/marketing-agents      ← pending (runs next)
#   plans/pipeline-safeguards   ← pending (runs after marketing-agents)
#   # comments and blank lines ignored
#
# To add a plan:  echo "plans/my-plan" >> plans/queue  then commit + push
# To skip a plan: prefix the line with #
# To re-run a done plan: delete its autoloop/<plan> branch and re-run
#
# Post-June-15-2026: set RUNNER=./run-once.sh in rolliq-autoloop.service
# and the queue runner picks it up automatically.

set -euo pipefail
cd "$(dirname "$0")"

QUEUE="plans/queue"
RUNNER="${RUNNER:-./run-batch.sh}"

# Read queue from origin/main so we don't need to be on main ourselves.
# Falls back to the local file if the remote isn't available.
QUEUE_CONTENT=$(git show "origin/main:$QUEUE" 2>/dev/null \
             || { [[ -f "$QUEUE" ]] && cat "$QUEUE"; } \
             || true)

if [[ -z "$QUEUE_CONTENT" ]]; then
    echo "No plans/queue found — nothing to do."
    exit 0
fi

is_done() {
    local plan="$1"
    local branch="autoloop/$(basename "$plan")"
    git show "origin/$branch:$plan/progress.md" 2>/dev/null | grep -q '^STATUS: DONE' \
    || git show "$branch:$plan/progress.md" 2>/dev/null | grep -q '^STATUS: DONE'
}

PLAN_DIR=""
while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line//[[:space:]]/}" ]] && continue
    plan="${line//[[:space:]]/}"
    if is_done "$plan"; then
        echo "skip (done): $plan"
        continue
    fi
    PLAN_DIR="$plan"
    break
done <<< "$QUEUE_CONTENT"

if [[ -z "$PLAN_DIR" ]]; then
    echo "All plans complete or queue empty — nothing to do."
    exit 0
fi

echo "Running: $PLAN_DIR"
exec env PLAN_DIR="$PLAN_DIR" "$RUNNER"
