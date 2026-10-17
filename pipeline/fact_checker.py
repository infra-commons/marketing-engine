"""
pipeline/fact_checker.py — Content Engine

Checks a generated draft for numerical and factual consistency.

Runs AFTER draft generation and BEFORE the compliance gate. Uses Claude
to verify:
  1. Internal arithmetic consistency — if the article says "X basis points
     from Y%", the resulting rate must add up.
  2. Brief faithfulness — key_facts from the brief are correctly represented
     (no numbers silently changed during generation).
  3. Cross-article contradictions — figures that contradict other published
     articles on the site (optional; pass existing_articles to enable).

Returns a FactCheckResult. Hard-fails on numerical errors (same philosophy
as compliance_gate.py). Warnings are advisory.

Usage:
    from pipeline.fact_checker import check as fact_check

    result = fact_check(draft_text, brief=brief)
    if not result.passed:
        raise FactCheckBlockedError(result)

    # or from the command line:
    python3 -m pipeline.fact_checker staging/drafts/draft-007-v2.md \\
        --brief staging/briefs/brief-007.json --brand cashbucket
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline.brand_loader import DEFAULT_BRAND, load_brand
from pipeline.claude_response import NoTextBlockError, reply_text

MODEL = "claude-sonnet-5-5"
# Headroom for adaptive thinking (which counts against this cap) plus a report the model
# may prefix with reasoning; a cut-off report cannot parse. The cap bills on tokens used,
# not on its size — see topic_selector.MAX_TOKENS.
MAX_TOKENS = 16000


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FactCheckResult:
    passed: bool
    flags: list[str] = field(default_factory=list)    # hard fails — block publication
    warnings: list[str] = field(default_factory=list)  # advisory only

    def summary(self) -> str:
        lines = ["FACT CHECK PASSED ✓" if self.passed else "FACT CHECK FAILED ✗"]
        if self.flags:
            lines.append(f"\nNumerical errors ({len(self.flags)}):")
            for f in self.flags:
                lines.append(f"  ✗ {f}")
        if self.warnings:
            lines.append(f"\nAdvisory warnings ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"  ⚠ {w}")
        return "\n".join(lines)


class FactCheckBlockedError(Exception):
    def __init__(self, result: FactCheckResult):
        self.result = result
        super().__init__(
            f"Fact check failed with {len(result.flags)} error(s):\n{result.summary()}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt(draft: str, brief: dict, existing_articles: list[str]) -> str:
    key_facts = json.dumps(brief.get("key_facts", []), indent=2)
    topic = brief.get("topic_statement", "")

    existing_context = ""
    if existing_articles:
        snippets = "\n\n---\n\n".join(existing_articles[:5])
        existing_context = f"""
## Existing published articles (check for contradictions)

{snippets}
"""

    return f"""You are a fact-checker for a financial content pipeline. Your job is to find
numerical errors, arithmetic inconsistencies, and contradictions — not to evaluate style.

## Brief key facts (source of truth for this article)

Topic: {topic}

{key_facts}
{existing_context}
## Draft article to check

{draft}

## Your task

Check the draft for the following and report any issues:

**HARD ERRORS (report as ERROR):**
1. Arithmetic inconsistencies — e.g. if the article says "X basis points from Y%", verify
   the resulting rate is correct. If it says OCR fell 325bp from 5.50%, the result must be
   2.25%, not 3.25%. Flag any case where the maths does not add up.
2. Numbers that contradict the brief's key_facts — e.g. if the brief says OCR is 2.25% but
   the article says 3.25%. The brief is the source of truth.
3. Numbers that contradict the existing published articles provided above.

**WARNINGS (report as WARNING):**
4. Numbers in the draft that do not appear anywhere in the brief key_facts and have no
   source cited — these may be hallucinated figures.
5. Dates that seem inconsistent with the brief's stated timeframe.

## Output format

Return ONLY a JSON object, with no text before or after it, in this exact structure:
{{
  "errors": [{{"description": "description of hard error 1", "is_error": true}}, ...],
  "warnings": ["description of warning 1", ...]
}}

Set is_error to true only for a confirmed hard error. If you checked something and it
turned out to be correct, either leave it out or set is_error to false — never list a
non-error as an error.
Be specific: quote the wrong number and state what it should be.
If everything checks out, return {{"errors": [], "warnings": []}}.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Response parsing
# ─────────────────────────────────────────────────────────────────────────────

def _extract_json(raw: str) -> dict | None:
    """The report object, even when the model wraps it in prose or a code fence."""
    decoder = json.JSONDecoder()
    start = raw.find("{")
    while start != -1:
        try:
            data, _ = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "errors" in data:
            return data
        start = raw.find("{", start + 1)
    return None


def _hard_errors(items: list) -> list[str]:
    """The confirmed errors. An item is dropped only when the model said is_error: false.

    The model sometimes lists reasoning that retracts itself ("No hard arithmetic errors
    are present"); counting that as an error discarded good drafts. Anything without an
    explicit false (a bare string, a missing flag) still counts, so this fails closed.
    """
    errors = []
    for item in items:
        if isinstance(item, dict):
            if item.get("is_error") is False:
                continue
            errors.append(str(item.get("description") or item))
        else:
            errors.append(str(item))
    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Core check function
# ─────────────────────────────────────────────────────────────────────────────

def check(
    draft: str,
    brief: dict,
    existing_articles: list[str] | None = None,
    api_key: str | None = None,
) -> FactCheckResult:
    """
    Run a fact-check on a draft against its brief.

    Args:
        draft:             Full article markdown text.
        brief:             Parsed brief dict (from brief JSON).
        existing_articles: Optional list of existing published article texts to
                           check for contradictions.
        api_key:           Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.

    Returns:
        FactCheckResult with passed, flags (hard errors), warnings.
    """
    key = api_key or os.environ.get("CB_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise EnvironmentError("CB_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY) not set — cannot run fact checker.")

    client = anthropic.Anthropic(api_key=key)
    prompt = _build_prompt(draft, brief, existing_articles or [])

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        raw = reply_text(response)
    except NoTextBlockError:
        # Thinking spent the whole cap. The checks below fail closed on an empty reply.
        raw = ""

    if getattr(response, "stop_reason", None) == "max_tokens":
        return FactCheckResult(
            passed=False,
            flags=[f"Fact checker response was truncated at {MAX_TOKENS} tokens: {raw[:200]}"],
        )

    data = _extract_json(raw)
    if data is None:
        # Claude didn't return valid JSON — treat as a checker error, not a pass
        return FactCheckResult(
            passed=False,
            flags=[f"Fact checker returned unparseable response: {raw[:200]}"],
        )

    errors = _hard_errors(data.get("errors") or [])
    warnings = data.get("warnings", [])
    passed = len(errors) == 0

    return FactCheckResult(passed=passed, flags=errors, warnings=warnings)


# ─────────────────────────────────────────────────────────────────────────────
# CLI entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fact-check a draft against its brief for numerical consistency.",
    )
    parser.add_argument("draft_path", help="Path to draft markdown file.")
    parser.add_argument("--brief", required=True, help="Path to brief JSON file.")
    parser.add_argument("--brand", default=DEFAULT_BRAND, help="Brand workspace.")
    parser.add_argument(
        "--existing-articles",
        nargs="*",
        help="Paths to existing published article HTML/MD files to check for contradictions.",
    )
    args = parser.parse_args()

    brand_cfg = load_brand(args.brand)

    draft_path = Path(args.draft_path)
    if not draft_path.is_absolute() and not draft_path.exists():
        draft_path = brand_cfg.brand_dir / args.draft_path
    draft = draft_path.read_text(encoding="utf-8")

    brief_path = Path(args.brief)
    if not brief_path.is_absolute() and not brief_path.exists():
        brief_path = brand_cfg.brand_dir / args.brief
    brief = json.loads(brief_path.read_text(encoding="utf-8"))

    existing_articles = []
    if args.existing_articles:
        for p in args.existing_articles:
            try:
                existing_articles.append(Path(p).read_text(encoding="utf-8"))
            except OSError:
                print(f"  ⚠  Could not read existing article: {p}", file=sys.stderr)

    api_key = os.environ.get("CB_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("❌  CB_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY) not set.", file=sys.stderr)
        return 1

    print(f"\n🔎  Fact-checking {draft_path.name} against {brief_path.name}...")
    result = check(draft, brief, existing_articles, api_key)

    print(f"\n{result.summary()}")

    if result.warnings:
        for w in result.warnings:
            print(f"  ⚠  {w}")

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
