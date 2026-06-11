#!/usr/bin/env bash
# loop.sh — autonomous plan-delivery loop.
#
# Usage:   ./loop.sh plans/<plan-name>
# Run from the repo root. Creates a branch autoloop/<plan-name> for review.
#
# Design: one prompt + a vision + a deterministic verifier:
#   pick task -> implement -> verify (oracle) -> commit if green -> repeat
#   until STATUS: DONE, max iterations, or budget cap.
set -euo pipefail

PLAN_DIR="${1:?Usage: ./loop.sh <plan-dir>   e.g. ./loop.sh plans/marketing-agents}"
PLAN_DIR="${PLAN_DIR%/}"

# --- config (override via env) ---
MAX_ITERS="${MAX_ITERS:-40}"
PER_CALL_BUDGET_USD="${PER_CALL_BUDGET_USD:-0.75}"
TOTAL_BUDGET_USD="${TOTAL_BUDGET_USD:-15}"
MODEL="${MODEL:-}"
BRANCH="${BRANCH:-autoloop/$(basename "$PLAN_DIR")}"

VISION="$PLAN_DIR/vision.md"
PROGRESS="$PLAN_DIR/progress.md"
VERIFY="$PLAN_DIR/verify.sh"
PROMPT="prompts/iterate.md"

# --- preflight ---
for f in "$VISION" "$VERIFY" "$PROMPT" constitution.md; do
    [[ -f "$f" ]] || { echo "missing required file: $f" >&2; exit 1; }
done
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "run inside a git repo" >&2; exit 1; }
chmod +x "$VERIFY"

git checkout -B "$BRANCH"

[[ -f "$PROGRESS" ]] || cat > "$PROGRESS" <<EOF
STATUS: IN_PROGRESS

# Progress — $(basename "$PLAN_DIR")

## Done

## In progress

## Blocked / needs human
EOF

spent="0"; i="0"
while (( i < MAX_ITERS )); do
    i=$((i+1))
    echo "=== iteration $i  (spent ~\$$spent) ==="

    model_flag=(); [[ -n "$MODEL" ]] && model_flag=(--model "$MODEL")

    result="$(claude -p "$(cat "$PROMPT")

PLAN_DIR=$PLAN_DIR" \
        "${model_flag[@]}" \
        --allowedTools "Read,Edit,Write,Bash" \
        --output-format json \
        --max-budget-usd "$PER_CALL_BUDGET_USD")" || echo "claude exited nonzero on iter $i (continuing)"

    call_cost="$(printf '%s' "$result" | jq -r '.total_cost_usd // 0' 2>/dev/null || echo 0)"
    spent="$(awk -v a="$spent" -v b="$call_cost" 'BEGIN{printf "%.4f", a+b}')"

    is_error="$(printf '%s' "$result" | jq -r '.is_error // false' 2>/dev/null || echo false)"
    subtype="$(printf '%s' "$result" | jq -r '.subtype // empty' 2>/dev/null || true)"
    if { [[ "$is_error" == "true" || "$subtype" == error* ]] ; } \
       && printf '%s' "$result" | grep -qiE 'credit|usage limit|quota|insufficient|exceeded'; then
        echo "Agent SDK credit exhausted — stopping. State on $BRANCH." >&2
        [[ -n "${SPEND_OUT:-}" ]] && printf '%s' "$spent" > "$SPEND_OUT"
        exit 75
    fi

    if bash "$VERIFY"; then
        git add -A
        git commit -q -m "autoloop($i): green — $(basename "$PLAN_DIR")" || echo "  (nothing to commit)"
        echo "  iter $i: GREEN"
    else
        echo "  iter $i: RED"
        { echo; echo "### iter $i: verify failed @ $(date -u +%FT%TZ)"; } >> "$PROGRESS"
    fi

    if grep -q '^STATUS: DONE' "$PROGRESS"; then
        echo "Definition of Done met — STATUS: DONE"; break
    fi
    if awk -v s="$spent" -v cap="$TOTAL_BUDGET_USD" 'BEGIN{exit !(s>=cap)}'; then
        echo "halt: total budget \$$TOTAL_BUDGET_USD reached. Likely mis-scoped." >&2
        break
    fi
done

[[ -n "${SPEND_OUT:-}" ]] && printf '%s' "$spent" > "$SPEND_OUT"
echo
echo "Loop finished: $i iterations, ~\$$spent."
echo "Review:  git log --oneline $BRANCH    /    git diff main...$BRANCH"
