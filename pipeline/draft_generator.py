"""
pipeline/draft_generator.py — Content Engine

Generates a compliant article draft from a brief JSON file.

Flow:
  1. Load brand config + phrase banks
  2. Load brief JSON
  3. Select from phrase banks (opener, transitions, CTA, close)
  4. Build prompt with: brief, article spine, phrase selections, structural rules
  5. Call Claude API (claude-opus-4-8) with prompt caching on the system prompt
  6. Run compliance gate on output
  7. If gate fails, retry once with targeted fix instructions
  8. Write to brands/{brand}/staging/review/draft-XXX-v1.md (or next version)

Usage:
    python3 -m pipeline.draft_generator staging/briefs/brief-001.json --brand cashbucket
    python3 -m pipeline.draft_generator staging/briefs/brief-006.json --brand cashbucket \\
        --output staging/review/draft-006-v1.md
    python3 -m pipeline.draft_generator staging/briefs/brief-001.json --brand cashbucket --dry-run
"""

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

import anthropic

# Add repo root so local modules import cleanly when run via -m or directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from banned_phrases import BANNED_PHRASES, LEVERAGE_SYNONYMS, DELVE_SYNONYMS, UNLOCK_SYNONYMS
from pipeline.brand_loader import DEFAULT_BRAND, BrandConfig, load_brand, load_phrase_banks
from pipeline.compliance_gate import GateResult, check as gate_check

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Default model is opus; a consumer can override per-run via the MODEL env var
# (e.g. a brand that runs on a cheaper tier sets MODEL=claude-sonnet-4-6).
MODEL = os.environ.get("MODEL") or "claude-opus-4-8"
MAX_TOKENS = 4000  # Opus produces denser output; extra headroom for retry

ALL_BANNED = BANNED_PHRASES + LEVERAGE_SYNONYMS + DELVE_SYNONYMS + UNLOCK_SYNONYMS

# Article type → Analysis section instructions (shared across brands)
ANALYSIS_INSTRUCTIONS = {
    "news-reaction": (
        "What does this development ACTUALLY mean for a NZ SME? Not what the press release says — "
        "the downstream effects on cash, margin, or operations. Commit to a view. Don't hedge with "
        "'it depends'. Give the reader something specific to act on."
    ),
    "explainer": (
        "Walk through the mechanism clearly. Use a specific example — real business type, real "
        "numbers, real NZ context. No jargon without definition. The reader should finish this section "
        "understanding the mechanism well enough to explain it to someone else."
    ),
    "how-to": (
        "4–7 numbered steps. Each step has a CONCRETE ACTION, not a principle. "
        "'Review your creditor terms' is a principle. "
        "'Contact your three largest suppliers and ask for 30-day terms if you're currently on 7-day' "
        "is an action. Vary sentence length — use short punchy sentences between longer ones."
    ),
    "sector-analysis": (
        "Specific to one sector. Real sector data. Named NZ companies or events where available. "
        "Connect the sector dynamics to cash flow explicitly. Have a view on what this sector "
        "should do differently — not generic advice that applies to any business."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Phrase bank selection
# ─────────────────────────────────────────────────────────────────────────────

def _select_phrases(brief: dict, pb) -> dict:
    """
    Select phrase bank items for this article.

    OPENER: All templates are passed to the model — it selects the best-fitting one
    AND fills {NZ_SPECIFIC} from the brief. Reason: the templates have different
    syntactic requirements (noun phrase vs event clause), so the model picks the
    appropriate template rather than randomly hitting an awkward match.

    TRANSITIONS, CTA, CLOSE: Pre-selected randomly and passed verbatim — these
    have no placeholder so random selection is safe.
    """
    transitions = random.sample(pb.TRANSITIONS, 4)
    cta = random.choice(pb.BRAND_CTAS)
    close = random.choice(pb.CLOSES)
    return {
        "openers_list": pb.OPENERS,
        "transitions": transitions,
        "cta": cta,
        "close": close,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ─────────────────────────────────────────────────────────────────────────────

def _build_banned_block() -> str:
    lines = ["BANNED PHRASES — NEVER USE ANY OF THESE (case-insensitive match = hard gate fail):"]
    for phrase in ALL_BANNED:
        lines.append(f"  ✗ {phrase}")
    return "\n".join(lines)


def _build_system_prompt(brand_cfg: BrandConfig) -> str:
    """
    Static system prompt — cached via prompt caching on every call.
    Brand-specific context injected from brand config.
    """
    return f"""You are a skilled business journalist writing articles for New Zealand small and medium enterprises (SMEs). Your articles appear on {brand_cfg.platform_description}.

VOICE AND STANDARDS:
- Specific: real NZ entities, real figures, real dates. Never vague generalities.
- Direct: say what you mean. Short sentences when directness matters.
- Credible: cite real sources by URL. Never invent statistics.
- Opinionated: commit to a view or trade-off rather than sitting on the fence.
- NZ-anchored: every article is grounded in NZ-specific context (RBNZ, IRD, Stats NZ, MBIE, Xero Small Business Insights, named NZ companies/regions).

HARD STRUCTURAL RULES (gate will fail if violated):
1. VARY sentence length aggressively. Include short sentences (under 8 words) as emphasis beats between longer analytical sentences. No more than 3 consecutive sentences in the 12–22 word range.
2. NEVER open consecutive paragraphs OR headings with the same word. Check every adjacent pair before outputting.
3. Sub-headings maximum 1 per 100 words.
4. {brand_cfg.brand_section_label} mention is in section 5 ONLY — one paragraph, soft, as one option among several.
5. Opening sentence MUST NOT restate the title.
6. Closing MUST NOT begin with "In conclusion", "To summarise", "To summarize", "As we have seen", or any restatement of the article structure.
7. Em-dashes (—): use sparingly. Maximum 30% of sentences may contain an em-dash.

ARTICLE STRUCTURE (fixed spine — sections 1–6):
1. Intro (150–200 words) — specific NZ hook, does NOT restate title
2. Context (200–300 words) — what's happening, min 2 NZ references with real figures, 1+ citation URL
3. Analysis (400–600 words) — varies by article type (see generation prompt)
4. So-what for NZ SMEs (200–250 words) — concrete actionable advice, not generic principles
5. {brand_cfg.brand_section_label} (1 paragraph, ~80 words) — soft, late, one option among several
6. Close (1–2 sentences) — earns the read, no summary

{_build_banned_block()}

OUTPUT FORMAT: Output ONLY the article in markdown. No preamble. No "Here is the article:" header. Start directly with the title as an H1 (# Title).
"""


def _build_generation_prompt(brief: dict, phrases: dict, brand_cfg: BrandConfig) -> str:
    """Per-article user prompt. Includes brief, phrase bank selections, and instructions."""
    brief_json = json.dumps(brief, indent=2)
    article_type = brief.get("article_type", "news-reaction")
    analysis_instruction = ANALYSIS_INSTRUCTIONS.get(
        article_type,
        "Provide specific, actionable analysis relevant to NZ SMEs. Commit to a view."
    )

    transitions_list = "\n".join(
        f'  {i + 1}. "{t}"' for i, t in enumerate(phrases["transitions"])
    )
    openers_numbered = "\n".join(
        f'  [{i + 1}] "{t}"' for i, t in enumerate(phrases["openers_list"])
    )
    brand_label_upper = brand_cfg.brand_section_label.upper()

    return f"""Generate a complete article from this brief.

## BRIEF
```json
{brief_json}
```

## ARTICLE TYPE: {article_type}
Analysis section (section 3) instruction:
{analysis_instruction}

## REQUIRED PHRASE BANK SELECTIONS
These phrases are hand-authored to eliminate AI signal. You must use them exactly as specified.

### OPENER (your article's very first sentence — section 1)
Choose the most appropriate template below for this brief. Replace {{NZ_SPECIFIC}} with a concise noun phrase drawn from the brief's key facts — keep it short (5–8 words), noun-phrase style (e.g. "the April 2026 OCR hold at 3.50%", "the minimum wage rise to $23.50/hr", "the August provisional tax instalment"). Do not use a full declarative sentence as the NZ_SPECIFIC.

Your first sentence MUST be one of these templates with {{NZ_SPECIFIC}} filled in. Use the template text verbatim — only replace {{NZ_SPECIFIC}}.

Opener options:
{openers_numbered}

### TRANSITIONS (use in sections 2–4, in any order, as paragraph connectors)
Use these 4 phrases verbatim as sentence starters or connectors within sections 2–4:
{transitions_list}

### {brand_label_upper} CTA (section 5 — use this entire paragraph verbatim)
"{phrases['cta']}"

### CLOSE (final sentence(s) of section 6 — use verbatim)
"{phrases['close']}"

## WORD COUNT TARGETS
- Total: 1200–1800 words
- Section 1 (Intro): 150–200 words
- Section 2 (Context): 200–300 words
- Section 3 (Analysis): 400–600 words
- Section 4 (So-what): 200–250 words
- Section 5 ({brand_cfg.brand_section_label}): ~80 words (use CTA above verbatim)
- Section 6 (Close): 1–2 sentences (use Close above verbatim)

## PRE-OUTPUT CHECKLIST — verify before outputting
Run through every item. If any fail, fix before outputting.
- [ ] First sentence is one of the opener templates above with {{NZ_SPECIFIC}} replaced by a concise noun phrase
- [ ] At least 3 distinct NZ entity/data-source references (RBNZ, IRD, Stats NZ, MBIE, named NZ companies, NZ regions, KiwiSaver, PAYE, GST, cash rate, OCR, etc.)
- [ ] At least 1 citation: a real URL from the brief's external_citations_required
- [ ] At least 1 specific figure: percentage to 2dp (e.g. 4.25%), dollar with cents, or exact date (e.g. 28 April 2026)
- [ ] No two adjacent paragraphs OR headings open with the same word
- [ ] Sentence lengths vary — at least 4–5 sentences under 8 words somewhere in the article
- [ ] Section 5 uses the {brand_cfg.brand_section_label} CTA paragraph above, verbatim
- [ ] Final sentence uses the Close above, verbatim
- [ ] No banned phrases from the system prompt
- [ ] Opening sentence does not restate the title
- [ ] No closing paragraph that begins with "In conclusion" or "To summarise"

Output the article now.
"""


def _build_retry_prompt(original_article: str, flags: list[str]) -> str:
    flags_formatted = "\n".join(f"  ✗ {flag}" for flag in flags)
    return f"""The article you generated failed the compliance gate with these specific issues:

GATE FAILURES:
{flags_formatted}

Fix ONLY these issues. Do not change content, facts, or phrase bank selections that already pass.
Keep the structure, NZ references, citations, and dates intact.

Output the complete revised article — start directly with # [Title], no preamble.

ORIGINAL ARTICLE TO REVISE:
{original_article}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Output path resolution
# ─────────────────────────────────────────────────────────────────────────────

def _auto_output_path(brief: dict, brand_cfg: BrandConfig) -> Path:
    """
    Derive output path from brief ID, incrementing version if file exists.
    brief-001 → brands/{brand}/staging/review/draft-001-v1.md
    """
    brief_id = brief.get("brief_id", "brief-000")
    match = re.search(r'(\d+)$', brief_id)
    num = match.group(1) if match else "000"

    v = 1
    while True:
        path = brand_cfg.draft_output_dir / f"draft-{num}-v{v}.md"
        if not path.exists():
            return path
        v += 1


# ─────────────────────────────────────────────────────────────────────────────
# Main generate function
# ─────────────────────────────────────────────────────────────────────────────

def generate(
    brief_path: str,
    brand_slug: str = DEFAULT_BRAND,
    output_path: str | None = None,
    verbose: bool = True,
    dry_run: bool = False,
) -> tuple[Path, GateResult]:
    """
    Generate a draft from a brief JSON file.

    Args:
        brief_path:  Path to the brief JSON file. If relative and not found as-is,
                     resolved against the brand's briefs directory.
        brand_slug:  Brand workspace to use (default: cashbucket).
        output_path: Optional output path. If None, auto-derives from brief ID.
        verbose:     Print progress to stdout.
        dry_run:     Generate and gate-check but don't write the output file.

    Returns:
        (output_path, gate_result)
    """
    # ── Load brand config + phrase banks ────────────────────────────────────
    brand_cfg = load_brand(brand_slug)
    pb = load_phrase_banks(brand_cfg.brand_dir)

    # ── Resolve brief path ────────────────────────────────────────────────
    bp = Path(brief_path)
    if not bp.is_absolute() and not bp.exists():
        bp = brand_cfg.brand_dir / brief_path
    brief = json.loads(bp.read_text(encoding="utf-8"))

    if verbose:
        print(f"\n📋  Brand:  {brand_cfg.display_name}")
        print(f"    Brief: {brief.get('brief_id')} — {brief.get('article_type', '?')}")
        print(f"    Topic: {brief.get('topic_statement', '')[:80]}...")

    # ── Select phrase bank items ─────────────────────────────────────────────
    phrases = _select_phrases(brief, pb)

    if verbose:
        print("\n📚  Phrase bank:")
        print(f"    Openers: {len(phrases['openers_list'])} templates (model selects)")
        print(f"    CTA:     {phrases['cta'][:60]}...")
        print(f"    Close:   {phrases['close'][:60]}...")

    # ── Check API key ────────────────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "\n❌  ANTHROPIC_API_KEY not set.\n"
            "    Export it before running:\n"
            "    export ANTHROPIC_API_KEY=sk-ant-...\n",
            file=sys.stderr,
        )
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # ── Build prompts ────────────────────────────────────────────────────────
    system_prompt = _build_system_prompt(brand_cfg)
    generation_prompt = _build_generation_prompt(brief, phrases, brand_cfg)

    # ── Attempt 1 ────────────────────────────────────────────────────────────
    if verbose:
        print(f"\n🤖  Calling {MODEL} (attempt 1)...")

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": generation_prompt}
        ],
    )

    article = response.content[0].text
    word_count = len(article.split())

    if verbose:
        print(f"    Generated {word_count} words")
        usage = response.usage
        print(
            f"    Usage — input: {usage.input_tokens}, output: {usage.output_tokens}, "
            f"cache_write: {getattr(usage, 'cache_creation_input_tokens', 0)}, "
            f"cache_read: {getattr(usage, 'cache_read_input_tokens', 0)}"
        )

    # ── Gate check (attempt 1) ───────────────────────────────────────────────
    title = brief.get("topic_statement", "")
    result = gate_check(article, title=title, brief=brief, brand_name=brand_cfg.display_name)

    if verbose:
        status = "PASS ✓" if result.passed else f"FAIL ✗ ({len(result.flags)} flag(s))"
        print(f"\n🔍  Gate (attempt 1): {status}")
        for flag in result.flags:
            print(f"    ✗ {flag}")
        for warn in result.warnings:
            print(f"    ⚠  {warn}")

    # ── Retry if failed ──────────────────────────────────────────────────────
    if not result.passed:
        if verbose:
            print("\n🔄  Retrying with targeted fixes...")

        retry_prompt = _build_retry_prompt(article, result.flags)

        retry_response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {"role": "user", "content": generation_prompt},
                {"role": "assistant", "content": article},
                {"role": "user", "content": retry_prompt},
            ],
        )

        article = retry_response.content[0].text
        word_count = len(article.split())
        result = gate_check(article, title=title, brief=brief, brand_name=brand_cfg.display_name)

        if verbose:
            print(f"    Generated {word_count} words")
            usage2 = retry_response.usage
            print(
                f"    Usage — input: {usage2.input_tokens}, output: {usage2.output_tokens}, "
                f"cache_write: {getattr(usage2, 'cache_creation_input_tokens', 0)}, "
                f"cache_read: {getattr(usage2, 'cache_read_input_tokens', 0)}"
            )
            status2 = "PASS ✓" if result.passed else f"FAIL ✗ ({len(result.flags)} flag(s))"
            print(f"\n🔍  Gate (attempt 2): {status2}")
            for flag in result.flags:
                print(f"    ✗ {flag}")
            for warn in result.warnings:
                print(f"    ⚠  {warn}")

    # ── Fact check (opt-in: brand sets workflow.fact_check) ───────────────────
    if brand_cfg.workflow.get("fact_check"):
        from pipeline.fact_checker import check as fact_check  # local: optional dep

        if verbose:
            print("\n🔎  Running fact check...")
        fc_result = fact_check(article, brief=brief, api_key=api_key)
        if verbose:
            fc_status = "PASS ✓" if fc_result.passed else f"FAIL ✗ ({len(fc_result.flags)} error(s))"
            print(f"    Fact check: {fc_status}")
            for flag in fc_result.flags:
                print(f"    ✗ {flag}")
            for warn in fc_result.warnings:
                print(f"    ⚠  {warn}")
        if not fc_result.passed:
            print(
                "\n❌  Fact check failed — draft has numerical errors. Do not publish.\n"
                "    Fix the brief's key_facts and regenerate.\n",
                file=sys.stderr,
            )
            sys.exit(1)

    # ── Resolve output path ──────────────────────────────────────────────────
    if output_path:
        out_path = Path(output_path)
        if not out_path.is_absolute():
            out_path = brand_cfg.brand_dir / output_path
    else:
        out_path = _auto_output_path(brief, brand_cfg)

    # ── Write output ─────────────────────────────────────────────────────────
    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(article, encoding="utf-8")
        if verbose:
            gate_label = "PASS ✓" if result.passed else "FAIL ✗ — route to operator review"
            print(f"\n✍   Written: {out_path}")
            print(f"    Gate:    {gate_label}")
    else:
        if verbose:
            gate_label = "PASS ✓" if result.passed else "FAIL ✗"
            print(f"\n🔍  Dry run — not written. Gate: {gate_label}")
            print(f"    Would write to: {out_path}")

    if not result.passed:
        if verbose:
            print(
                "\n⚠   Gate still failing after 2 attempts. "
                "Route to operator review — do not publish."
            )

    return out_path, result


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a compliant article draft from a brief JSON file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m pipeline.draft_generator staging/briefs/brief-001.json --brand cashbucket
  python3 -m pipeline.draft_generator staging/briefs/brief-006.json --brand rolliq \\
      --output staging/review/draft-006-v1.md
  python3 -m pipeline.draft_generator staging/briefs/brief-001.json --brand cashbucket --dry-run
        """,
    )
    parser.add_argument("brief", help="Path to the brief JSON file")
    parser.add_argument(
        "--brand",
        default=DEFAULT_BRAND,
        help=f"Brand workspace to use (default: {DEFAULT_BRAND})",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output path for the draft (default: auto-derived from brief ID)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and gate-check but don't write the output file",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    args = parser.parse_args()

    out_path, gate_result = generate(
        brief_path=args.brief,
        brand_slug=args.brand,
        output_path=args.output,
        verbose=not args.quiet,
        dry_run=args.dry_run,
    )

    sys.exit(0 if gate_result.passed else 1)
