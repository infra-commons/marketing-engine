"""
pipeline/queue_manager.py — Publish queue manager

Reads and updates brands/{brand}/staging/publish_queue.json.

Usage:
    # Print the next queued item as GitHub Actions env vars
    python3 -m pipeline.queue_manager next --brand cashbucket --output-env

    # Mark an item as published
    python3 -m pipeline.queue_manager mark-published staging/approved/draft-003-v1.md --brand cashbucket

    # Print a human-readable summary of the queue
    python3 -m pipeline.queue_manager status --brand cashbucket
"""

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

# Add repo root so local imports work when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.brand_loader import DEFAULT_BRAND, BrandConfig, load_brand


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
    Return a warning string if this is a news-reaction article without
    dates_verified=true in its brief. Returns None if no warning needed.
    """
    draft_path = cfg.resolve_draft_path(item["draft_path"])
    m = re.search(r"draft-(\d+)-", draft_path.name)
    if not m:
        return None
    brief_path = cfg.briefs_dir / f"brief-{m.group(1)}.json"
    if not brief_path.exists():
        return None
    try:
        with brief_path.open(encoding="utf-8") as f:
            brief = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if brief.get("article_type") != "news-reaction":
        return None
    if brief.get("dates_verified", False):
        return None
    slug = item.get("slug", draft_path.name)
    return (
        f"DATES NOT VERIFIED: '{slug}' is a news-reaction article. "
        f"Set dates_verified=true in its brief after confirming all dates against primary sources."
    )


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
                return 1
            item["status"] = "approved"
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
    if cfg.approval_model == "status":
        print(f"  Approved (ready to publish):  {approved}")
        print(f"  Queued (needs approval):      {queued}")
        print(f"  On hold:                      {on_hold}")
        next_approved = next((q for q in queue if q.get("status") == "approved"), None)
        next_queued = next((q for q in queue if q.get("status") == "queued"), None)
        if next_approved:
            print(f"\n  Next publish: {next_approved.get('slug')}  [approved]")
        elif next_queued:
            print(f"\n  Next up (needs approval): {next_queued.get('slug')}")
            print(
                f"  → Approve: python3 -m pipeline.queue_manager --brand {args.brand} "
                f"approve {next_queued.get('slug')}"
            )
        else:
            print("\n  Nothing queued — add articles to resume cadence.")
    else:
        print(f"  Queued:         {queued}")
        if pending:
            print(f"  Pending review: {pending}")

    print(f"\n{'#':<4} {'Status':<22} {'Draft':<32} Slug")
    print("-" * 95)
    for i, item in enumerate(queue, 1):
        status = item.get("status", "queued")
        draft = Path(item["draft_path"]).name
        slug = item.get("slug", "")
        pub_date = item.get("published_at", "")
        status_str = f"published {pub_date}" if pub_date else status
        print(f"{i:<4} {status_str:<22} {draft:<32} {slug}")
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

    # approve (status-flag workflow)
    p_approve = sub.add_parser("approve", help="Approve a queued article for the next publish run")
    p_approve.add_argument("slug", help="Article slug as stored in publish_queue.json")
    p_approve.add_argument(
        "--push",
        action="store_true",
        help="Commit and push the approval to the remote immediately",
    )
    p_approve.set_defaults(func=cmd_approve)

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
