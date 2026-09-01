"""
pipeline/queue_manager.py — Publish queue manager

Reads and updates brands/{brand}/staging/publish_queue.json.

Usage (``<brand>`` is a workspace under brands/):
    # Print the next queued item as GitHub Actions env vars
    python3 -m pipeline.queue_manager next --brand <brand> --output-env

    # Mark an item as published
    python3 -m pipeline.queue_manager mark-published staging/approved/draft-003-v1.md --brand <brand>

    # Print a human-readable summary of the queue, with each entry's gate verdict
    python3 -m pipeline.queue_manager status --brand <brand>

    # Return a pending_review/hold entry to the cadence (clears its stale verdict)
    python3 -m pipeline.queue_manager requeue <slug> --brand <brand>

An entry carries a recorded verdict — ``gate_passed``, ``dates_verified``, ``gate_stale``
— and ``pipeline.queue_policy`` is the single predicate that reads it. See that module for
why absence is not failure and why an override must carry a written reason.
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Add repo root so local imports work when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.brand_loader import DEFAULT_BRAND, BrandConfig, load_brand
from pipeline.queue_policy import describe, recorded_blockers


def load_queue(brand_slug: str) -> list[dict]:
    cfg = load_brand(brand_slug)
    if not cfg.queue_path.exists():
        return []
    with cfg.queue_path.open(encoding="utf-8") as f:
        return json.load(f)


def save_queue(brand_slug: str, queue: list[dict]) -> None:
    cfg = load_brand(brand_slug)
    with cfg.queue_path.open("w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _check_dates_verified(item: dict, cfg: BrandConfig) -> str | None:
    """
    Return a warning string if this article's brief lacks dates_verified=true.
    Returns None if no warning is needed.

    This used to fire only for ``article_type: news-reaction``, on the reasoning that a
    reaction piece is the one anchored to dates. That reasoning does not survive contact
    with the failure it is meant to catch: an unverified figure is a wrong statement about
    the world whatever genre the article is filed under, and a sector analysis quoting an
    adoption percentage carries exactly the same exposure as a news reaction quoting a
    date. In practice the narrower scope meant the check had never fired at all — the
    article types actually being produced were all outside it.

    Falls back to the entry's own recorded ``dates_verified`` when the brief cannot be
    read, so a missing or unparseable brief does not silently disable the check. A guard
    that answers "no problem" when it could not look is the failure mode this whole area
    exists to remove.
    """
    recorded = item.get("dates_verified")
    draft_path = cfg.resolve_draft_path(item["draft_path"])
    slug = item.get("slug", draft_path.name)

    def warn() -> str:
        return (
            f"DATES NOT VERIFIED: '{slug}'. The compliance gate scores prose, not truth, so "
            "nothing downstream will catch a wrong figure. Set dates_verified=true in its "
            "brief after confirming every date and number against a primary source."
        )

    brief = None
    m = re.search(r"draft-(\d+)-", draft_path.name)
    if m:
        brief_path = cfg.briefs_dir / f"brief-{m.group(1)}.json"
        if brief_path.exists():
            try:
                with brief_path.open(encoding="utf-8") as f:
                    brief = json.load(f)
            except (json.JSONDecodeError, OSError):
                brief = None

    if brief is not None:
        return None if brief.get("dates_verified", False) else warn()

    # No readable brief: fall back to what the queue recorded, and stay silent only when
    # it positively says the figures were checked.
    return None if recorded is True else warn()


def _load_brief(draft_path: str, cfg: BrandConfig) -> dict | None:
    """The brief behind a draft, resolved by the draft's number. None if unreadable.

    Matched on the filename's NNN rather than on a slug because the slug is optional in a
    brief and the number is how the two have always been paired on disk.
    """
    m = re.search(r"draft-(\d+)-", cfg.resolve_draft_path(draft_path).name)
    if not m:
        return None
    brief_path = cfg.briefs_dir / f"brief-{m.group(1)}.json"
    if not brief_path.exists():
        return None
    try:
        with brief_path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _approver() -> str:
    """Who is approving, as well as this process can tell.

    QUEUE_APPROVER lets a caller supply a verified identity (a web handler has one from
    its auth layer); otherwise the shell user, which at least distinguishes an interactive
    approval from an automated one.
    """
    return os.environ.get("QUEUE_APPROVER") or os.environ.get("USER") or "cli"


def _utc_stamp() -> str:
    """An ISO-8601 UTC timestamp in the same shape a browser's toISOString() produces.

    Matched byte-for-byte on purpose: consuming repositories write `gate_override` stamps
    from a web handler as well as from this CLI, and a reader should not have to know
    which one produced a given entry in order to parse its timestamp.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _emit_warning(message: str) -> None:
    """Emit a warning to stderr. In GitHub Actions, also emit as a workflow annotation."""
    print(f"⚠  {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{message}", file=sys.stderr)


def cmd_next(args: argparse.Namespace) -> int:
    cfg = load_brand(args.brand)
    queue = load_queue(args.brand)
    # Dir-move workflow publishes the next "queued" item directly. Status-flag
    # brands gate publishing behind an explicit approve step, so "next" is the
    # first item already moved to "approved".
    ready_status = "approved" if cfg.approval_model == "status" else "queued"
    item = next((q for q in queue if q.get("status") == ready_status), None)

    if item is None:
        if cfg.approval_model == "status":
            pending = sum(1 for q in queue if q.get("status") == "queued")
            if pending:
                msg = (
                    f"No approved articles — {pending} queued and waiting for approval. "
                    f"Approve via dashboard or: "
                    f"python3 -m pipeline.queue_manager --brand {args.brand} approve <slug>"
                )
                print(msg)
                if os.environ.get("GITHUB_ACTIONS"):
                    print(f"::notice::{msg}")
            else:
                print("Queue is empty — nothing to publish.")
        else:
            print("Queue is empty — nothing to publish.")
        if args.output_env:
            print("QUEUE_DRAFT_PATH=")
            print("QUEUE_SLUG=")
            print("QUEUE_DESCRIPTION=")
        return 0

    # Dates verification check — warn before publishing, not after
    warning = _check_dates_verified(item, cfg)
    if warning:
        _emit_warning(warning)

    # The recorded verdict is reported here and does NOT withhold the entry. Three
    # behaviours were possible and the other two are both worse:
    #
    #   exit nonzero  — the caller's queue lookup propagates it, and the scheduled run
    #                   goes red before the publisher can produce a message that names
    #                   the article and the reason.
    #   withhold      — emit empty keys and exit 0. That is a green run indistinguishable
    #                   from an empty queue, repeating every cycle, for an article that is
    #                   sitting at the head of the queue blocked. It would also make the
    #                   caller's "nothing to publish" notice print the wrong remedy.
    #   warn + emit   — this. `next` answers "what is at the head of the queue", not "may
    #                   it ship". Two components deciding publishability, from different
    #                   inputs, is what produced this class of bug in the first place.
    #
    # The refusal lives in `publisher` (before any file is written) and in the caller's
    # own preflight. This is the preview, not the alarm.
    for reason in recorded_blockers(item):
        _emit_warning(f"QUEUE VERDICT: '{item.get('slug', '')}' — {reason}")

    if args.output_env:
        slug = item.get("slug", "")
        description = item.get("description", "")
        description = description.replace("%", "%25").replace("\n", "%0A").replace("\r", "%0D")
        print(f"QUEUE_DRAFT_PATH={item['draft_path']}")
        print(f"QUEUE_SLUG={slug}")
        print(f"QUEUE_DESCRIPTION={description}")
    else:
        print(json.dumps(item, indent=2, ensure_ascii=False))

    return 0


def cmd_mark_published(args: argparse.Namespace) -> int:
    queue = load_queue(args.brand)
    target = args.draft_path

    found = False
    for item in queue:
        if item["draft_path"] == target:
            item["status"] = "published"
            item["published_at"] = str(date.today())
            found = True
            break

    if not found:
        print(f"ERROR: No queue item found for draft_path='{target}'", file=sys.stderr)
        return 1

    save_queue(args.brand, queue)
    print(f"Marked as published: {target}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Enqueue a freshly generated draft in the held ('queued') state.

    This is stage-1 of the two-stage weekly cadence: the Tuesday draft run
    registers its new draft here as ``queued`` — which, under the status-flag
    approval model, is NOT publishable until a human runs ``approve`` (the
    sign-off gate). Idempotent: a draft_path already in the queue is left as-is.
    """
    cfg = load_brand(args.brand)
    queue = load_queue(args.brand)
    target = args.draft_path

    existing = next((q for q in queue if q.get("draft_path") == target), None)
    if existing is not None:
        print(f"Already queued (status '{existing.get('status')}'): {target}")
        return 0

    entry = {
        "draft_path": target,
        "slug": args.slug or "",
        "description": args.description or "",
        "status": "queued",
        "added_at": str(date.today()),
    }
    # Record the compliance verdict so the human approver sees it before sign-off (the
    # draft is still enqueued — the human gate is the real block, not the CI gate).
    #
    # Written unconditionally, both ways. Recording only failures made absence ambiguous:
    # a missing key meant "passed" OR "added by something that never ran the gate" — a
    # hand edit, or a bare `add` — and every reader had to treat the two the same way,
    # which meant treating an ungated entry as a clean one. Now absence means exactly one
    # thing: the entry predates this change.
    entry["gate_passed"] = not getattr(args, "gate_failed", False)

    # `dates_verified` is a property of the brief, and the brief is what the gate cannot
    # check: it scores prose, not truth. Copied here so every consumer has the record,
    # including those that do not run a separate re-gate step. Omitted with a warning when
    # the brief cannot be found — writing True on a miss would assert that figures were
    # verified when nothing looked, which is the exact failure this key exists to prevent.
    brief = _load_brief(entry["draft_path"], cfg)
    if brief is None:
        print(
            f"  ⚠ no brief found for {target} — dates_verified not recorded. "
            "Nothing has confirmed this article's figures against a primary source.",
            file=sys.stderr,
        )
    else:
        entry["dates_verified"] = bool(brief.get("dates_verified", False))

    queue.append(entry)
    save_queue(args.brand, queue)

    gate_note = "  ⚠ compliance gate FAILED — review carefully" if entry.get("gate_passed") is False else ""
    print(f"Queued (held, needs approval): {entry['slug'] or target}{gate_note}")
    if cfg.approval_model == "status":
        print(
            f"  → Approve to publish: python3 -m pipeline.queue_manager --brand {args.brand} "
            f"approve {entry['slug'] or '<slug>'}"
        )
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve a queued article (status-flag workflow): queued -> approved."""
    queue = load_queue(args.brand)
    slug = args.slug

    found = False
    for item in queue:
        if item.get("slug") == slug:
            current = item.get("status")
            if current == "approved":
                print(f"Already approved: {slug}")
                return 0
            if current != "queued":
                print(
                    f"ERROR: Cannot approve '{slug}' — status is '{current}' (must be 'queued').",
                    file=sys.stderr,
                )
                if current in ("pending_review", "hold"):
                    print(
                        f"  → To bring it back into the cadence: python3 -m pipeline.queue_manager "
                        f"--brand {args.brand} requeue {slug}",
                        file=sys.stderr,
                    )
                return 1

            # The recorded verdict, which until now nothing on this path read. Approving
            # is the moment a human takes responsibility for an article, so it is the
            # right place to put what is known about it in front of them — not the
            # publish run days later, by which point the decision has already been made.
            blockers = recorded_blockers(item)
            if blockers and not getattr(args, "force", False):
                print(f"ERROR: Cannot approve '{slug}' on its recorded verdict:", file=sys.stderr)
                for reason in blockers:
                    print(f"  • {reason}", file=sys.stderr)
                print(
                    "\n  Fix the draft and re-run the gate, verify the brief's figures, or "
                    "approve anyway with --force --reason '<why>'.",
                    file=sys.stderr,
                )
                return 1

            if blockers:
                reason = getattr(args, "reason", "") or ""
                if not reason.strip():
                    print("ERROR: --force requires --reason.", file=sys.stderr)
                    return 1
                # Same shape a web approval handler writes, so a reader does not need to
                # know which one produced a given stamp.
                item["gate_override"] = {
                    "by": _approver(),
                    "reason": reason,
                    "at": _utc_stamp(),
                    "blockers": blockers,
                }

            item["status"] = "approved"
            # Recorded on every approval, not only overrides. Without it `git log` on the
            # queue file cannot answer "who approved this", which is the first question
            # asked about anything that shipped and should not have.
            item["approved_by"] = _approver()
            item["approved_at"] = _utc_stamp()
            found = True
            break

    if not found:
        print(f"ERROR: No queue item found with slug='{slug}'", file=sys.stderr)
        return 1

    save_queue(args.brand, queue)
    print(f"Approved: {slug}")
    print("It will be published on the next publish run.")

    if getattr(args, "push", False):
        import subprocess
        cfg = load_brand(args.brand)
        # publish_queue.json -> staging -> <brand> -> brands -> consumer repo root
        repo_root = cfg.queue_path.parents[3]
        queue_rel = str(cfg.queue_path.relative_to(repo_root))
        try:
            subprocess.run(["git", "add", queue_rel], cwd=repo_root, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"queue: approve {slug}"],
                cwd=repo_root, check=True,
            )
            subprocess.run(["git", "pull", "--rebase"], cwd=repo_root, check=True)
            subprocess.run(["git", "push"], cwd=repo_root, check=True)
            print("Pushed approval to remote.")
        except subprocess.CalledProcessError as e:
            print(f"ERROR: git operation failed: {e}", file=sys.stderr)
            return 1

    return 0


# Everything `requeue` clears. Each of these describes a specific version of a specific
# draft; carrying any of them across a requeue would describe the wrong article.
_VERDICT_KEYS = (
    "gate_passed",
    "gate_flags",
    "gate_warnings",
    "dates_verified",
    "word_count",
    "gate_override",
    "approved_by",
    "approved_at",
)


def cmd_requeue(args: argparse.Namespace) -> int:
    """Bring an entry back into the cadence: pending_review | hold -> queued.

    `pending_review` and `hold` had no exit. Nothing wrote them and nothing could move
    them: approve refuses any status but `queued`, `next` selects only the ready status,
    and a status listing counted them without offering an action. An entry set to either
    was out of the pipeline permanently, short of hand-editing the JSON — which meant a
    queue could appear stalled on its schedule when it was actually stalled on a status
    with no transition.

    The safety property this has to preserve: requeueing must not be a way to reach
    `approved` without passing the gate. It CLEARS the recorded verdict, because that
    verdict describes a draft that is about to be revised — and then, because absence is
    deliberately read as "unknown" rather than "failed", it sets `gate_stale` so the
    cleared entry still blocks. Recording `gate_passed: false` instead would be a lie: the
    gate did not fail, it did not run. Re-run the gate and the flag is replaced with a
    real verdict.

    Deliberately narrow. It refuses `published` (which would republish) and `approved`
    (un-approving is a different verb with different consequences); it is idempotent on
    `queued`.
    """
    queue = load_queue(args.brand)
    slug = args.slug

    for item in queue:
        if item.get("slug") != slug and item.get("draft_path") != slug:
            continue

        current = item.get("status")
        if current == "queued":
            print(f"Already queued: {slug}")
            return 0
        if current not in ("pending_review", "hold"):
            print(
                f"ERROR: Cannot requeue '{slug}' — status is '{current}' "
                "(must be 'pending_review' or 'hold').",
                file=sys.stderr,
            )
            return 1

        cleared = [key for key in _VERDICT_KEYS if key in item]
        for key in cleared:
            del item[key]

        item["status"] = "queued"
        item["requeued_at"] = str(date.today())
        if getattr(args, "reason", ""):
            item["requeued_reason"] = args.reason
        # The entry is now un-gated, and must read as such to every consumer.
        item["gate_stale"] = True

        save_queue(args.brand, queue)
        print(f"Requeued (held, needs re-gating then approval): {slug}")
        if cleared:
            print(f"  Cleared {len(cleared)} stale verdict field(s): {', '.join(cleared)}")
        print(
            "  It is marked gate_stale and cannot be approved until the gate has been re-run "
            "against the current draft."
        )
        return 0

    print(f"ERROR: No queue item found with slug='{slug}'", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    queue = load_queue(args.brand)
    cfg = load_brand(args.brand)
    if not queue:
        print(f"Queue is empty for brand: {cfg.display_name}")
        return 0

    total = len(queue)
    published = sum(1 for q in queue if q.get("status") == "published")
    queued = sum(1 for q in queue if q.get("status") == "queued")
    approved = sum(1 for q in queue if q.get("status") == "approved")
    pending = sum(1 for q in queue if q.get("status") == "pending_review")
    on_hold = sum(1 for q in queue if q.get("status") == "hold")

    print(f"\n{cfg.display_name} publish queue: {published}/{total} published")
    blocked = sum(1 for q in queue if recorded_blockers(q))
    if cfg.approval_model == "status":
        print(f"  Approved (ready to publish):  {approved}")
        print(f"  Queued (needs approval):      {queued}")
        print(f"  On hold:                      {on_hold}")
        # Printed in this branch too. It used to appear only under the dir-move model, so
        # a status-model brand with entries stranded at `pending_review` was told nothing
        # about them at all — the one place that counted them never showed the count.
        print(f"  Pending review:               {pending}")
        if blocked:
            print(f"  Blocked by recorded verdict:  {blocked}")
        if pending or on_hold:
            print(
                f"  → Bring one back: python3 -m pipeline.queue_manager --brand {args.brand} "
                "requeue <slug>"
            )
        next_approved = next((q for q in queue if q.get("status") == "approved"), None)
        next_queued = next((q for q in queue if q.get("status") == "queued"), None)
        if next_approved:
            print(f"\n  Next publish: {next_approved.get('slug')}  [approved]")
        elif next_queued:
            print(f"\n  Next up (needs approval): {next_queued.get('slug')}")
            # The approve nudge used to be unconditional, with no hint that the entry it
            # names carries blockers. An instruction that will fail when followed is worse
            # than none: it teaches that the refusal is noise.
            reasons = recorded_blockers(next_queued)
            for reason in reasons:
                print(f"    ⚠ {reason}")
            suffix = " --force --reason '<why>'" if reasons else ""
            print(
                f"  → Approve: python3 -m pipeline.queue_manager --brand {args.brand} "
                f"approve {next_queued.get('slug')}{suffix}"
            )
        else:
            print("\n  Nothing queued — add articles to resume cadence.")
    else:
        print(f"  Queued:         {queued}")
        if pending:
            print(f"  Pending review: {pending}")

    print(f"\n{'#':<4} {'Status':<22} {'Gate':<8} {'Draft':<32} Slug")
    print("-" * 104)
    for i, item in enumerate(queue, 1):
        status = item.get("status", "queued")
        draft = Path(item["draft_path"]).name
        slug = item.get("slug", "")
        pub_date = item.get("published_at", "")
        status_str = f"published {pub_date}" if pub_date else status
        # 'ok' / 'GATE' / 'DATES' / 'STALE' / '-' (never gated). The verdict was recorded
        # on every entry and shown on none of them.
        print(f"{i:<4} {status_str:<22} {describe(item):<8} {draft:<32} {slug}")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the publish queue.")
    parser.add_argument(
        "--brand",
        default=DEFAULT_BRAND,
        help=f"Brand workspace (default: {DEFAULT_BRAND})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # next
    p_next = sub.add_parser("next", help="Get the next queued item")
    p_next.add_argument(
        "--output-env",
        action="store_true",
        help="Output as GitHub Actions env var lines",
    )
    p_next.set_defaults(func=cmd_next)

    # add (enqueue a freshly generated draft in the held 'queued' state)
    p_add = sub.add_parser("add", help="Enqueue a draft in the held ('queued') state")
    p_add.add_argument("draft_path", help="Draft path relative to brand dir (e.g. staging/drafts/draft-011-v1.md)")
    p_add.add_argument("--slug", default="", help="URL slug for the article")
    p_add.add_argument("--description", default="", help="Meta description (~155 chars)")
    p_add.add_argument(
        "--gate-failed",
        dest="gate_failed",
        action="store_true",
        help="Mark the draft as having failed the compliance gate (flags it for the human approver)",
    )
    p_add.set_defaults(func=cmd_add)

    # approve (status-flag workflow)
    p_approve = sub.add_parser("approve", help="Approve a queued article for the next publish run")
    p_approve.add_argument("slug", help="Article slug as stored in publish_queue.json")
    p_approve.add_argument(
        "--push",
        action="store_true",
        help="Commit and push the approval to the remote immediately",
    )
    p_approve.add_argument(
        "--force",
        action="store_true",
        help="Approve despite the entry's recorded blockers. Requires --reason.",
    )
    p_approve.add_argument(
        "--reason",
        default="",
        help="Why the recorded blockers are being overridden. Required with --force; stored on the entry.",
    )
    p_approve.set_defaults(func=cmd_approve)

    # requeue (the exit from pending_review / hold, which previously had none)
    p_requeue = sub.add_parser(
        "requeue",
        help="Return a pending_review or hold article to 'queued' for re-gating and approval",
    )
    p_requeue.add_argument("slug", help="Article slug (or draft_path) as stored in publish_queue.json")
    p_requeue.add_argument(
        "--reason",
        default="",
        help="Why it is being brought back. Stored on the entry as requeued_reason.",
    )
    p_requeue.set_defaults(func=cmd_requeue)

    # mark-published
    p_mark = sub.add_parser("mark-published", help="Mark a draft as published")
    p_mark.add_argument("draft_path", help="Draft path as stored in queue (e.g. staging/approved/draft-003-v1.md)")
    p_mark.set_defaults(func=cmd_mark_published)

    # status
    p_status = sub.add_parser("status", help="Print queue summary")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
