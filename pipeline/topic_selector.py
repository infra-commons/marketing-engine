"""
pipeline/topic_selector.py — Trending-topic → brief synthesizer (Tuesday, stage 1)

Stage 1 of the two-stage weekly cadence. Picks a timely topic from the brand's
trending feeds, then synthesizes a full article brief JSON from it via Claude, so
the unattended Tuesday run has something topical to draft (rather than a static,
pre-canned choice).

Explainable + overridable by design (the DoD's "not fully opaque" requirement):
  - Feeds are a human-controlled list in brand.yaml (`topic_feeds:`); a sensible
    NZ-SME default is used when a brand sets none.
  - Every run writes a selection record to staging/topics/topic-<date>.json listing
    all candidates considered and the rationale for the winner.
  - A human can override the whole selection with `--topic "<statement>"` (skips the
    feed entirely), swap feeds with `--feeds url1,url2`, or edit brand.yaml.

Deduping: candidates whose topic overlaps an already-published slug or an existing
brief's topic_statement are dropped, so the same story isn't drafted twice.

Usage:
    python3 -m pipeline.topic_selector --brand rolliq
    python3 -m pipeline.topic_selector --brand rolliq --dry-run
    python3 -m pipeline.topic_selector --brand rolliq --topic "RBNZ holds the OCR at 3.50% — what it means for SME cash flow"
    python3 -m pipeline.topic_selector --brand rolliq --feeds "https://news.google.com/rss/search?q=...&hl=en-NZ&gl=NZ&ceid=NZ:en"
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.brand_loader import DEFAULT_BRAND, BrandConfig, load_brand

MODEL = os.environ.get("MODEL") or "claude-opus-4-8"

# NZ-SME business-news default feeds, used when a brand sets no `topic_feeds:`.
# Google News RSS search needs no API key and returns reverse-chronological
# (freshest-first) results scoped to NZ. A brand overrides these in brand.yaml.
DEFAULT_FEEDS = [
    "https://news.google.com/rss/search?q=New+Zealand+small+business+when:14d&hl=en-NZ&gl=NZ&ceid=NZ:en",
    "https://news.google.com/rss/search?q=New+Zealand+SME+economy+OR+tax+OR+cashflow+when:14d&hl=en-NZ&gl=NZ&ceid=NZ:en",
]

VALID_ARTICLE_TYPES = ("news-reaction", "explainer", "how-to", "sector-analysis")

# Output token cap for the brief-synthesis call. RETRY_MAX_TOKENS is used only for the
# one retry after a confirmed truncation (stop_reason == "max_tokens"), so the common
# case's cost is unchanged and the bump is evidence-gated rather than a blind raise of
# the ceiling — see infra-commons/marketing-engine#22.
MAX_TOKENS = 2000
RETRY_MAX_TOKENS = 4000

# Words too generic to signal topic overlap on their own.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "by", "with",
    "what", "why", "how", "when", "your", "you", "nz", "new", "zealand", "business",
    "businesses", "sme", "smes", "it", "is", "are", "that", "this", "from", "as",
}


# ─────────────────────────────────────────────────────────────────────────────
# Feed fetching + parsing (stdlib only — no feedparser/requests dependency)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_feed(url: str, timeout: int = 15) -> str:
    """Fetch a feed URL and return its raw body. Isolated for test monkeypatching."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "infra-commons-marketing-engine/topic-selector"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https feeds)
        return resp.read().decode("utf-8", errors="replace")


def _localname(tag: str) -> str:
    return tag.split("}")[-1]


def _child_text(el, name: str) -> str:
    for child in el:
        if _localname(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _entry_link(el) -> str:
    # RSS: <link>url</link>; Atom: <link href="url"/>
    for child in el:
        if _localname(child.tag) == "link":
            if child.text and child.text.strip():
                return child.text.strip()
            href = child.attrib.get("href")
            if href:
                return href.strip()
    return ""


def parse_feed(xml_text: str, per_feed_cap: int = 12) -> list[dict]:
    """Parse RSS 2.0 or Atom into a list of {title, link, summary} dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[dict] = []
    for el in root.iter():
        tag = _localname(el.tag)
        if tag not in ("item", "entry"):
            continue
        title = _child_text(el, "title")
        if not title:
            continue
        summary = _child_text(el, "description") or _child_text(el, "summary")
        items.append(
            {
                "title": _strip_html(title),
                "link": _entry_link(el),
                "summary": _strip_html(summary)[:400],
            }
        )
        if len(items) >= per_feed_cap:
            break
    return items


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Candidate collection + dedupe
# ─────────────────────────────────────────────────────────────────────────────

def _brand_feeds(cfg: BrandConfig, override: list[str] | None) -> list[str]:
    if override:
        return override
    configured = cfg.workflow.get("topic_feeds") or []
    return list(configured) if configured else list(DEFAULT_FEEDS)


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _covered_topics(cfg: BrandConfig) -> list[set[str]]:
    """Keyword sets for topics already published or already briefed, for dedupe."""
    covered: list[set[str]] = []
    # Published/queued slugs + descriptions
    if cfg.queue_path.exists():
        try:
            queue = json.loads(cfg.queue_path.read_text(encoding="utf-8"))
            for q in queue:
                covered.append(_keywords(f"{q.get('slug', '')} {q.get('description', '')}"))
        except (json.JSONDecodeError, OSError):
            pass
    # Existing briefs' topic statements
    if cfg.briefs_dir.exists():
        for bf in cfg.briefs_dir.glob("brief-*.json"):
            try:
                b = json.loads(bf.read_text(encoding="utf-8"))
                covered.append(_keywords(b.get("topic_statement", "")))
            except (json.JSONDecodeError, OSError):
                continue
    return [c for c in covered if c]


def _overlaps(candidate_kw: set[str], covered: list[set[str]], threshold: float = 0.6) -> bool:
    """True if the candidate's keywords substantially overlap an already-covered topic."""
    if not candidate_kw:
        return False
    for cov in covered:
        if not cov:
            continue
        shared = len(candidate_kw & cov)
        # Jaccard-ish: shared over the smaller set, so a short slug still matches.
        if shared / min(len(candidate_kw), len(cov)) >= threshold:
            return True
    return False


def collect_candidates(
    cfg: BrandConfig, feeds: list[str], limit: int = 15
) -> list[dict]:
    """Fetch all feeds, flatten, drop title-dupes and already-covered topics."""
    covered = _covered_topics(cfg)
    seen_titles: set[str] = set()
    candidates: list[dict] = []
    for url in feeds:
        try:
            raw = _fetch_feed(url)
        except Exception as e:  # a dead feed must not kill the run
            print(f"⚠  feed fetch failed ({url}): {e}", file=sys.stderr)
            continue
        for item in parse_feed(raw):
            key = item["title"].lower()
            if key in seen_titles:
                continue
            kw = _keywords(item["title"])
            if _overlaps(kw, covered):
                continue
            seen_titles.add(key)
            candidates.append(item)
            if len(candidates) >= limit:
                return candidates
    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# Brief synthesis (Claude)
# ─────────────────────────────────────────────────────────────────────────────

def _slugify(text: str, max_words: int = 9) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:max_words]) or "untitled"


def _next_brief_id(cfg: BrandConfig) -> str:
    nums = [0]
    if cfg.briefs_dir.exists():
        for bf in cfg.briefs_dir.glob("brief-*.json"):
            m = re.search(r"brief-(\d+)", bf.name)
            if m:
                nums.append(int(m.group(1)))
    return f"brief-{max(nums) + 1:03d}"


def _synthesis_prompt(cfg: BrandConfig, candidates: list[dict], override_topic: str | None) -> str:
    if override_topic:
        source_block = (
            "The operator has supplied the topic directly (no feed selection needed):\n"
            f"  TOPIC: {override_topic}\n"
        )
    else:
        lines = ["Pick the single BEST candidate for a timely SME article, then write its brief.\n"]
        lines.append("CANDIDATE HEADLINES (freshest-first):")
        for i, c in enumerate(candidates):
            lines.append(f"  [{i}] {c['title']}")
            if c.get("summary"):
                lines.append(f"       {c['summary'][:200]}")
            if c.get("link"):
                lines.append(f"       source: {c['link']}")
        source_block = "\n".join(lines)

    return f"""You are the editorial lead for {cfg.display_name}, which publishes for {cfg.platform_description}.

{source_block}

Produce ONE article brief as a single JSON object and OUTPUT ONLY THE JSON (no prose, no code fences).

Choose the candidate (or use the supplied topic) most relevant to a New Zealand SME
audience and most likely to make a genuinely useful, non-generic article. Prefer a
timely angle with real downstream consequences for cash, margin, tax, or operations.

The JSON MUST have exactly these keys:
- "brief_id": leave as "PENDING" (the caller assigns the real id)
- "created_at": "{date.today()}"
- "status": "auto-generated"
- "topic_statement": one or two sentences framing the specific NZ SME angle
- "article_type": one of {list(VALID_ARTICLE_TYPES)}
- "target_audience": who this is for (specific NZ SME segments)
- "key_facts": a list of 2-4 objects, each {{"fact": "...", "source": "https://..."}} — use
  REAL, verifiable NZ sources (RBNZ, IRD, Stats NZ, MBIE, Privacy Commissioner, the linked
  article). Do not invent statistics or URLs; if unsure of a figure, describe the claim
  qualitatively rather than fabricating a number.
- "angle": the specific, committed point of view the article takes
- "{cfg.brand}_connection": one sentence on how {cfg.display_name} relates to this topic
- "word_count_target": an integer 1200-1500
- "external_citations_required": a list of 1-2 real URLs drawn from key_facts
- "{cfg.brand}_mention_guide": one sentence on how to mention {cfg.display_name} (brief, near the end; not a pitch)
- "notes": guidance for the writer, INCLUDING an explicit instruction to verify every date and
  figure against the primary source before publish
- "dates_verified": false
- "source_url": the chosen candidate's source link (or "" for an operator-supplied topic)
- "source_title": the chosen candidate's headline (or the supplied topic)
- "selection_rationale": one sentence explaining why this candidate was chosen over the others

Output the JSON now."""


class TruncatedResponseError(RuntimeError):
    """The model's response was cut off by the max_tokens cap (stop_reason == "max_tokens"),
    not malformed generation. Callers should treat this as a distinct, retriable condition
    from a JSON parse failure — see infra-commons/marketing-engine#22."""


def _call_claude(prompt: str, api_key: str, max_tokens: int = MAX_TOKENS) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "max_tokens":
        raise TruncatedResponseError(
            f"model response was truncated at the {max_tokens}-token cap "
            f"(stop_reason=max_tokens) — the JSON brief is incomplete, not malformed."
        )
    return resp.content[0].text


def _extract_json(text: str) -> dict:
    """Parse a JSON object out of the model output, tolerating stray fences/prose."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def synthesize_brief(
    cfg: BrandConfig,
    candidates: list[dict],
    override_topic: str | None,
    api_key: str,
) -> dict:
    """Call Claude to select a candidate (or use the override) and return a brief dict."""
    prompt = _synthesis_prompt(cfg, candidates, override_topic)
    try:
        raw = _call_claude(prompt, api_key)
    except TruncatedResponseError as e:
        print(
            f"⚠  {e} Retrying once with max_tokens={RETRY_MAX_TOKENS}…",
            file=sys.stderr,
        )
        raw = _call_claude(prompt, api_key, max_tokens=RETRY_MAX_TOKENS)
    brief = _extract_json(raw)

    # Normalise / harden the model output.
    brief["brief_id"] = _next_brief_id(cfg)
    brief.setdefault("created_at", str(date.today()))
    brief.setdefault("status", "auto-generated")
    at = brief.get("article_type")
    if at not in VALID_ARTICLE_TYPES:
        brief["article_type"] = "news-reaction"
    brief["dates_verified"] = False  # auto-generated → never pre-verified
    brief.setdefault("slug", _slugify(brief.get("topic_statement", brief["brief_id"])))
    brief.setdefault(
        "description",
        (brief.get("angle") or brief.get("topic_statement", ""))[:155],
    )
    return brief


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def select_and_write(
    brand_slug: str,
    override_topic: str | None = None,
    feeds_override: list[str] | None = None,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict:
    """Select a topic, synthesize a brief, and (unless dry-run) write it to briefs/.

    Returns the brief dict (with an added 'brief_path' key when written).
    """
    cfg = load_brand(brand_slug)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌  ANTHROPIC_API_KEY not set — cannot synthesize a brief.", file=sys.stderr)
        sys.exit(1)

    candidates: list[dict] = []
    if not override_topic:
        feeds = _brand_feeds(cfg, feeds_override)
        if verbose:
            print(f"🔎  Fetching {len(feeds)} feed(s) for {cfg.display_name}…")
        candidates = collect_candidates(cfg, feeds)
        if not candidates:
            print(
                "❌  No fresh, non-duplicate candidates found across the configured feeds.\n"
                "    Supply a topic directly with --topic, or widen topic_feeds in brand.yaml.",
                file=sys.stderr,
            )
            sys.exit(2)
        if verbose:
            print(f"    {len(candidates)} candidate topic(s) after dedupe.")

    try:
        brief = synthesize_brief(cfg, candidates, override_topic, api_key)
    except TruncatedResponseError as e:
        print(f"❌  Brief synthesis failed: {e}", file=sys.stderr)
        sys.exit(3)
    except json.JSONDecodeError as e:
        print(
            f"❌  Brief synthesis failed: model response was not valid JSON ({e}). "
            "This is a malformed-generation failure, not truncation — the remedy is a "
            "prompt/schema fix, not a higher token cap.",
            file=sys.stderr,
        )
        sys.exit(3)

    if verbose:
        print(f"\n📝  Brief {brief['brief_id']} — {brief.get('article_type')}")
        print(f"    Topic: {brief.get('topic_statement', '')[:90]}")
        print(f"    Source: {brief.get('source_title', override_topic or '')[:80]}")
        print(f"    Why:   {brief.get('selection_rationale', '(operator-supplied topic)')[:90]}")

    if dry_run:
        if verbose:
            print("\n🔍  Dry run — brief not written.")
        return brief

    # Write the brief + an explainability record of the selection.
    cfg.briefs_dir.mkdir(parents=True, exist_ok=True)
    brief_path = cfg.briefs_dir / f"{brief['brief_id']}.json"
    brief_path.write_text(json.dumps(brief, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    cfg.topics_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "date": str(date.today()),
        "brief_id": brief["brief_id"],
        "chosen": {
            "title": brief.get("source_title", override_topic or ""),
            "url": brief.get("source_url", ""),
            "rationale": brief.get("selection_rationale", ""),
        },
        "override_topic": override_topic or None,
        "candidates_considered": candidates,
    }
    (cfg.topics_dir / f"topic-{date.today()}.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    brief["brief_path"] = str(brief_path.relative_to(cfg.brand_dir))
    if verbose:
        print(f"\n✍   Wrote brief:   {brief_path}")
        print(f"    Selection log: {cfg.topics_dir / f'topic-{date.today()}.json'}")
    return brief


def main() -> int:
    p = argparse.ArgumentParser(description="Select a trending topic and synthesize an article brief.")
    p.add_argument("--brand", default=DEFAULT_BRAND, help=f"Brand workspace (default: {DEFAULT_BRAND})")
    p.add_argument("--topic", default=None, help="Override: use this topic statement directly (skip feeds)")
    p.add_argument("--feeds", default=None, help="Override feeds (comma-separated URLs)")
    p.add_argument("--dry-run", action="store_true", help="Select + synthesize but do not write the brief")
    args = p.parse_args()

    feeds_override = [u.strip() for u in args.feeds.split(",")] if args.feeds else None
    select_and_write(
        args.brand,
        override_topic=args.topic,
        feeds_override=feeds_override,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
