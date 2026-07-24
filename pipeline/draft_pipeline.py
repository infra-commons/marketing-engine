"""
pipeline/draft_pipeline.py — Tuesday draft stage (the whole stage-1 in one command)

The two-stage weekly cadence's Tuesday step, as a single entrypoint a scheduled
workflow can call unattended:

    1. select a trending topic + synthesize a brief   (pipeline.topic_selector)
    2. generate a compliant article draft             (pipeline.draft_generator)
    3. enqueue the draft in the HELD ('queued') state  (pipeline.queue_manager add)

The result sits in the queue as ``queued`` — NOT publishable. A human must run
``queue_manager approve <slug>`` (the sign-off gate) before the Thursday publish
step will touch it. This module never approves and never publishes.

Usage:
    python3 -m pipeline.draft_pipeline --brand rolliq
    python3 -m pipeline.draft_pipeline --brand rolliq --topic "RBNZ holds OCR at 3.50% …"
    python3 -m pipeline.draft_pipeline --brand rolliq --brief brief-011   # reuse an existing brief
    python3 -m pipeline.draft_pipeline --brand rolliq --dry-run
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import draft_generator, topic_selector
from pipeline.brand_loader import DEFAULT_BRAND, load_brand
from pipeline.queue_manager import cmd_add


def run(
    brand_slug: str,
    override_topic: str | None = None,
    existing_brief: str | None = None,
    feeds_override: list[str] | None = None,
    dry_run: bool = False,
) -> int:
    cfg = load_brand(brand_slug)

    if cfg.approval_model != "status":
        # A gate only exists under the status model (queue_manager `next` withholds
        # non-approved items). Refuse rather than silently produce a publishable draft.
        print(
            f"❌  Brand '{brand_slug}' uses approval model '{cfg.approval_model}', not 'status'.\n"
            f"    The two-stage sign-off gate requires `workflow.approval: status` in brand.yaml.",
            file=sys.stderr,
        )
        return 3

    # ── Stage 1a: obtain a brief (existing, or select+synthesize a fresh one) ──
    if existing_brief:
        brief_rel = existing_brief if existing_brief.endswith(".json") else f"staging/briefs/{existing_brief}.json"
        brief_path = cfg.brand_dir / brief_rel
        if not brief_path.exists():
            print(f"❌  Brief not found: {brief_path}", file=sys.stderr)
            return 1
        import json

        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        print(f"📄  Using existing brief {brief.get('brief_id', existing_brief)}")
    else:
        brief = topic_selector.select_and_write(
            brand_slug,
            override_topic=override_topic,
            feeds_override=feeds_override,
            dry_run=dry_run,
        )
        brief_rel = brief.get("brief_path", f"staging/briefs/{brief['brief_id']}.json")

    if dry_run:
        print("\n🔍  Dry run — stopping before draft generation.")
        return 0

    # ── Stage 1b: generate the draft ──────────────────────────────────────────
    print(f"\n🤖  Generating draft for {brief['brief_id']}…")
    out_path, gate_result = draft_generator.generate(
        brief_rel, brand_slug=brand_slug, verbose=True
    )
    draft_rel = str(out_path.relative_to(cfg.brand_dir))

    # ── Stage 1c: enqueue in the HELD state (needs human sign-off) ────────────
    add_args = argparse.Namespace(
        brand=brand_slug,
        draft_path=draft_rel,
        slug=brief.get("slug", ""),
        description=brief.get("description", ""),
        gate_failed=not gate_result.passed,
    )
    cmd_add(add_args)

    print(
        f"\n✅  Stage 1 complete. '{brief.get('slug')}' is QUEUED and awaiting sign-off.\n"
        f"    Nothing publishes until you approve it:\n"
        f"      python3 -m pipeline.queue_manager --brand {brand_slug} approve {brief.get('slug')}"
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Tuesday draft stage: topic → brief → draft → held queue.")
    p.add_argument("--brand", default=DEFAULT_BRAND, help=f"Brand workspace (default: {DEFAULT_BRAND})")
    p.add_argument("--topic", default=None, help="Override the trending selection with this topic statement")
    p.add_argument("--brief", default=None, help="Reuse an existing brief id (e.g. brief-011) instead of selecting")
    p.add_argument("--feeds", default=None, help="Override trending feeds (comma-separated URLs)")
    p.add_argument("--dry-run", action="store_true", help="Select/synthesize only; do not draft or enqueue")
    args = p.parse_args()

    # Env fallbacks let a CI workflow pass untrusted dispatch inputs (a free-text
    # topic, a brief id) via the environment instead of interpolating them into a
    # shell command line — avoiding command injection. An empty env value is ignored.
    topic = args.topic or (os.environ.get("DRAFT_TOPIC") or None)
    brief = args.brief or (os.environ.get("DRAFT_BRIEF") or None)
    feeds_env = os.environ.get("DRAFT_FEEDS")
    feeds_src = args.feeds or (feeds_env or None)
    feeds_override = [u.strip() for u in feeds_src.split(",")] if feeds_src else None
    return run(
        args.brand,
        override_topic=topic,
        existing_brief=brief,
        feeds_override=feeds_override,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
