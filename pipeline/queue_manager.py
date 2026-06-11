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
    item = next((q for q in queue if q.get("status") == "queued"), None)

    if item is None:
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


def cmd_status(args: argparse.Namespace) -> int:
    queue = load_queue(args.brand)
    cfg = load_brand(args.brand)
    if not queue:
        print(f"Queue is empty for brand: {cfg.display_name}")
        return 0

    total = len(queue)
    published = sum(1 for q in queue if q.get("status") == "published")
    queued = sum(1 for q in queue if q.get("status") == "queued")
    pending = sum(1 for q in queue if q.get("status") == "pending_review")

    print(f"\n{cfg.display_name} publish queue: {published}/{total} published, {queued} queued, {pending} pending review\n")
    print(f"{'#':<4} {'Status':<12} {'Draft':<30} {'Slug'}")
    print("-" * 90)
    for i, item in enumerate(queue, 1):
        status = item.get("status", "queued")
        draft = Path(item["draft_path"]).name
        slug = item.get("slug", "")
        pub_date = item.get("published_at", "")
        status_str = f"{status}" if not pub_date else f"published {pub_date}"
        print(f"{i:<4} {status_str:<22} {draft:<30} {slug}")
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
