You are working autonomously on ONE plan inside this repo.

Read these files first, every single time:
1. constitution.md           — non-negotiable rules. Obey them.
2. $PLAN_DIR/vision.md       — the goal and the Definition of Done.
3. $PLAN_DIR/progress.md     — what has happened so far.

Then do exactly this:
- Pick the SINGLE highest-value incomplete item that moves toward the Definition of Done.
- Implement just that one item. Small, reviewable diff.
- Add or update tests so the work is verifiable. Never fabricate data or NZ/AU
  references — every figure must trace to a real, publicly verifiable source.
- Run the plan's checks ($PLAN_DIR/verify.sh) and fix anything you broke.
- Update $PLAN_DIR/progress.md: move the item to Done, note new items under
  "Blocked / needs human", and record any assumption you had to make.

Stop signal:
- If EVERY item in the Definition of Done is met AND verify.sh passes, set the top
  line of $PLAN_DIR/progress.md to exactly:  STATUS: DONE
- Otherwise leave it as:  STATUS: IN_PROGRESS

Do not attempt the whole plan in one turn. One unit of work, verified, recorded.
