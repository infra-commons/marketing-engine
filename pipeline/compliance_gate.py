"""
Content Engine — Compliance Gate

THE SINGLE SOURCE OF PASS/FAIL AUTHORITY.

Every draft passes through check() before publication.
Failure = hard stop. Not a warning. Not a log line.

Returns a GateResult. Caller must check result.passed before proceeding.
The gate does not call the publisher. Ever.

Usage:
    from pipeline.compliance_gate import check

    result = check(text, brief=brief, brand_name="Cashbucket")
    if not result.passed:
        # route to operator review queue with result.flags listed
        raise PublicationBlockedError(result)

    # only reach here if gate passed
    publisher.publish(text)
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Add parent dir so we can import banned_phrases from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))
from banned_phrases import (
    BANNED_PHRASES,
    EM_DASH_CHARS,
    LEVERAGE_SYNONYMS,
    DELVE_SYNONYMS,
    UNLOCK_SYNONYMS,
    TITLE_EM_DASH_CHARS,
    TITLE_CLICHES,
)

ALL_BANNED = BANNED_PHRASES + LEVERAGE_SYNONYMS + DELVE_SYNONYMS + UNLOCK_SYNONYMS

# ─────────────────────────────────────────────────────────────────────────────
# NZ-specific reference patterns
# ─────────────────────────────────────────────────────────────────────────────

NZ_ENTITY_PATTERNS = [
    r'\bRBNZ\b',
    r'Reserve Bank of New Zealand',
    r'\bIRD\b',
    r'Inland Revenue',
    r'\bMBIE\b',
    r'Stats NZ',
    r'Statistics New Zealand',
    r'BusinessNZ',
    r'BusinessDesk',
    r'NZ Herald',
    r'New Zealand Herald',
    r'\bXero\b',
    r'cash rate',
    r'OCR\b',
    r'provisional tax',
    r'GST\b',
    r'PAYE\b',
    r'KiwiSaver',
    r'ACC\b',
    r'\bAuckland\b', r'\bWellington\b', r'\bChristchurch\b', r'\bHamilton\b',
    r'\bTauranga\b', r'\bDunedin\b', r'\bPalmerston North\b', r'\bNelson\b',
    r'\bWaikato\b', r'\bBay of Plenty\b', r'\bOtago\b', r'\bMarlborough\b',
    r'\bHawke\'s Bay\b', r'\bManawatū\b', r'\bSouthland\b',
    r'\bFonterra\b', r'\bSpark\b', r'\bFletcher\b', r'\bSanford\b',
    r'\bDebtfix\b', r'\bConnectworks\b',
]

CITATION_PATTERNS = [
    # NZ government and regulatory
    r'https?://(?:www\.)?stats\.govt\.nz',
    r'https?://(?:www\.)?rbnz\.govt\.nz',
    r'https?://(?:www\.)?ird\.govt\.nz',
    r'https?://(?:www\.)?mbie\.govt\.nz',
    r'https?://(?:www\.)?treasury\.govt\.nz',
    r'https?://(?:www\.)?msd\.govt\.nz',
    r'https?://(?:www\.)?privacy\.org\.nz',
    # NZ news and business
    r'https?://(?:www\.)?businessdesk\.co\.nz',
    r'https?://(?:www\.)?nzherald\.co\.nz',
    r'https?://(?:www\.)?stuff\.co\.nz',
    r'https?://(?:www\.)?businessnz\.org\.nz',
    # AU government and regulatory
    r'https?://(?:www\.)?abs\.gov\.au',
    r'https?://(?:www\.)?ato\.gov\.au',
    r'https?://(?:www\.)?rba\.gov\.au',
    r'https?://(?:www\.)?treasury\.gov\.au',
    r'https?://(?:www\.)?asic\.gov\.au',
    r'https?://(?:www\.)?apra\.gov\.au',
    # AU news and business (named in PLAN.md)
    r'https?://(?:www\.)?afr\.com',
    r'https?://(?:www\.)?smartcompany\.com\.au',
    # AU professional bodies
    r'https?://(?:www\.)?cpaaustralia\.com\.au',
    r'https?://(?:www\.)?fpa\.com\.au',
    r'https?://(?:www\.)?charteredaccountantsanz\.com',
    # Cross-market (NZ+AU)
    r'https?://(?:www\.)?xero\.com',
]

SPECIFIC_NUMBER_PATTERNS = [
    r'\d+\.\d{2}%',
    r'\$\d[\d,]*\.\d{2}\b',
    r'\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\s+\d{4}\b',
    r'\b(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\s+\d{1,2},?\s+\d{4}\b',
    r'\b\d{4}-\d{2}-\d{2}\b',
    r'\$[\d,]+(?:\.\d+)?\s*(?:million|billion|thousand)\b',
    r'\b\d+(?:\.\d+)?\s*(?:million|billion)\s*(?:NZD|dollars)\b',
]

# Date extraction patterns — used for brief grounding check
_DATE_EXTRACT_PATTERNS = [
    re.compile(
        r'\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+\d{4})\b',
        re.IGNORECASE,
    ),
    re.compile(
        r'\b((?:January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+\d{1,2},?\s+\d{4})\b',
        re.IGNORECASE,
    ),
    re.compile(r'\b(\d{4}-\d{2}-\d{2})\b'),
]


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GateResult:
    passed: bool
    flags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)   # advisory — does NOT block

    def summary(self) -> str:
        lines = ["GATE PASSED ✓" if self.passed else "GATE FAILED ✗"]
        if self.flags:
            lines.append(f"\nHard fails ({len(self.flags)}):")
            for f in self.flags:
                lines.append(f"  ✗ {f}")
        if self.warnings:
            lines.append(f"\nAdvisory warnings ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"  ⚠ {w}")
        return "\n".join(lines)


class PublicationBlockedError(Exception):
    def __init__(self, result: GateResult):
        self.result = result
        super().__init__(f"Compliance gate failed with {len(result.flags)} flag(s):\n{result.summary()}")


# ─────────────────────────────────────────────────────────────────────────────
# Check functions — each returns list of flag strings (empty = pass)
# ─────────────────────────────────────────────────────────────────────────────

def _check_lexical(text: str) -> list[str]:
    """Hard fail: any banned phrase present (case-insensitive)."""
    lower = text.lower()
    found = [phrase for phrase in ALL_BANNED if phrase in lower]
    return [f"Banned phrase: '{phrase}'" for phrase in found]


def _check_title(title: str) -> list[str]:
    """
    Hard fail: AI tells in the headline itself.

    The title is rendered verbatim into <title>/<h1>/<h2>/social-meta/image-alt,
    so it is screened directly — the body checks never see it. Three rules:
      1. No em dash (em/en dash or &mdash;) — a strong AI tell in a headline,
         even though body prose may use it.
      2. No banned lexical phrase (reuses ALL_BANNED).
      3. No explainer-cliché tail ("… — Here's Why It Matters", "What You Need
         to Know", etc.).
    """
    flags = []
    if not title:
        return flags

    for char in TITLE_EM_DASH_CHARS:
        if char in title:
            flags.append(f"Title contains em/en dash ('{char}') — AI tell in headlines")
            break

    lower = title.lower()
    flags += [f"Title banned phrase: '{phrase}'" for phrase in ALL_BANNED if phrase in lower]
    flags += [f"Title explainer cliché: '{cliche}'" for cliche in TITLE_CLICHES if cliche in lower]

    return flags


def _check_structural(text: str) -> list[str]:
    """Hard fail: structural AI tells."""
    flags = []
    sentences = _split_sentences(text)
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]

    if not sentences:
        return ["No sentences found — empty or malformed text"]

    # 1. Em-dash as primary break: >30% of sentences
    em_dash_count = sum(1 for s in sentences if any(c in s for c in EM_DASH_CHARS) or ' -- ' in s)
    em_dash_pct = em_dash_count / len(sentences)
    if em_dash_pct > 0.30:
        flags.append(
            f"Em-dash overuse: {em_dash_pct:.0%} of sentences use em-dash as primary break (limit 30%)"
        )

    # 2. Sentence length band: >50% within 12–22 words
    word_counts = [len(s.split()) for s in sentences if len(s.split()) > 2]
    if word_counts:
        in_band = sum(1 for wc in word_counts if 12 <= wc <= 22)
        band_pct = in_band / len(word_counts)
        if band_pct > 0.50:
            flags.append(
                f"Sentence length uniformity: {band_pct:.0%} of sentences within 12–22 word band (limit 50%)"
            )

    # 3. Sub-heading frequency: more than 1 per 100 words
    word_count = len(text.split())
    heading_count = len(re.findall(r'^#{1,4}\s+\S', text, re.MULTILINE))
    if heading_count > 0 and word_count / heading_count < 100:
        flags.append(
            f"Sub-heading density: 1 heading per {word_count // heading_count} words (minimum 100)"
        )

    # 4. Three or more consecutive paragraphs with same opening word
    if len(paragraphs) >= 3:
        for i in range(len(paragraphs) - 2):
            first_words = [_first_word(paragraphs[i + j]) for j in range(3)]
            if len(set(first_words)) == 1 and first_words[0]:
                flags.append(
                    f"3+ consecutive paragraphs start with '{first_words[0]}' — structural parallel"
                )
                break

    # 5. Closing paragraph starts with summary opener
    if paragraphs:
        last_para = paragraphs[-1].lower().lstrip('#').strip()
        summary_openers = ('in conclusion', 'to conclude', 'in summary', 'to summarise',
                           'to summarize', 'as we have seen', 'as outlined above')
        for opener in summary_openers:
            if last_para.startswith(opener):
                flags.append(f"Closing paragraph starts with summary opener: '{opener}'")

    return flags


def _check_positive_markers(text: str) -> list[str]:
    """Hard fail: required NZ-specific content missing."""
    flags = []

    nz_matches = set()
    for pattern in NZ_ENTITY_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            nz_matches.add(pattern)
    if len(nz_matches) < 3:
        flags.append(
            f"Insufficient NZ-specific references: {len(nz_matches)} found, 3 required "
            f"(need named NZ entities, RBNZ/IRD/Stats NZ figures, or NZ regions)"
        )

    has_citation = any(re.search(p, text, re.IGNORECASE) for p in CITATION_PATTERNS)
    if not has_citation:
        flags.append(
            "No citation found — at least one real NZ source URL required "
            "(stats.govt.nz, rbnz.govt.nz, ird.govt.nz, mbie.govt.nz, nzherald.co.nz, etc.)"
        )

    has_specific_number = any(re.search(p, text) for p in SPECIFIC_NUMBER_PATTERNS)
    if not has_specific_number:
        flags.append(
            "No specific numeric figure found — need at least one of: "
            "percentage to 2dp (e.g. 4.25%), dollar amount with cents, or exact date (e.g. 28 March 2026)"
        )

    return flags


def _check_brand_mention(text: str, brand_name: str) -> tuple[list[str], list[str]]:
    """
    Returns (hard_flags, warnings).
    Hard fail: brand mentioned as a hard sell.
    Warning: brand not mentioned, or mentioned too early.
    """
    hard_flags = []
    warnings = []

    brand_lower = brand_name.lower()
    lower = text.lower()
    brand_count = lower.count(brand_lower)

    if brand_count == 0:
        warnings.append(
            f"{brand_name} not mentioned — expected one soft mention in the final third"
        )

    # Hard sell patterns (brand-agnostic)
    hard_sell_patterns = [
        rf'{brand_lower} is the (?:best|only|ultimate|leading)',
        rf'{brand_lower} will (?:transform|revolutionise|revolutionize)',
        rf'(?:sign up|try|start) (?:{brand_lower} )?(?:today|now|free)',
        rf'{brand_lower} (?:guarantees?|ensures?|makes? sure)',
        rf'the (?:best|only|right) (?:tool|solution|choice) (?:for|is) {brand_lower}',
    ]
    for pattern in hard_sell_patterns:
        if re.search(pattern, lower):
            hard_flags.append(f"{brand_name} hard-sell language detected: matches '{pattern}'")

    if brand_count > 0:
        first_mention_pos = lower.find(brand_lower)
        if first_mention_pos < len(text) * 0.5:
            warnings.append(
                f"{brand_name} first appears at {first_mention_pos / len(text):.0%} into the article "
                f"— should be in the final third"
            )

    if brand_count > 4:
        hard_flags.append(
            f"{brand_name} mentioned {brand_count} times — maximum 4 (one paragraph, soft)"
        )

    return hard_flags, warnings


# ─────────────────────────────────────────────────────────────────────────────
# Date grounding check
# ─────────────────────────────────────────────────────────────────────────────

def _extract_article_dates(text: str) -> list[str]:
    """Extract all specific dates (day+month+year) from the article."""
    dates: set[str] = set()
    for pattern in _DATE_EXTRACT_PATTERNS:
        for match in pattern.finditer(text):
            dates.add(match.group(1).strip())
    return sorted(dates)


def _brief_text(brief: dict) -> str:
    """Recursively collect all string values from a brief dict."""
    parts: list[str] = []

    def _collect(obj) -> None:
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _collect(v)
        elif isinstance(obj, list):
            for item in obj:
                _collect(item)

    _collect(brief)
    return " ".join(parts)


def _check_date_grounding(text: str, brief: dict) -> list[str]:
    """
    Advisory warning: every specific date in the article should appear
    verbatim in the brief. An ungrounded date means the model invented
    or misquoted a fact not in the source material.

    This is a WARNING, not a hard fail — there are legitimate reasons a
    correct date might not be in the brief (e.g. added for context).
    But every warning must be manually verified before the article publishes.
    """
    warnings = []
    article_dates = _extract_article_dates(text)
    if not article_dates:
        return warnings

    brief_body = _brief_text(brief).lower()
    for date_str in article_dates:
        if date_str.lower() not in brief_body:
            warnings.append(
                f"Ungrounded date: '{date_str}' appears in article but not in brief — "
                f"verify this is correct before publishing"
            )
    return warnings


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _split_sentences(text: str) -> list[str]:
    clean = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    clean = re.sub(r'^\s*[-*]\s+', '', clean, flags=re.MULTILINE)
    sentences = re.split(r'(?<=[.!?])\s+', clean)
    return [s.strip() for s in sentences if len(s.strip()) > 10]


def _first_word(paragraph: str) -> str:
    clean = re.sub(r'^#{1,6}\s+', '', paragraph)
    words = clean.strip().split()
    return words[0].lower().rstrip('.,;:') if words else ''


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def check(
    text: str,
    title: str = "",
    brief: dict | None = None,
    brand_name: str = "Cashbucket",
) -> GateResult:
    """
    Run all compliance checks. Returns GateResult.

    Hard fails → result.passed = False, flags in result.flags
    Advisory   → result.warnings (does not affect passed)

    Args:
        text:       The full article text (markdown or plain text).
        title:      Optional. If provided, checks opening sentence doesn't restate title.
        brief:      Optional. If provided, runs date grounding check against brief facts.
        brand_name: Brand name for the soft-mention check (default: "Cashbucket").
    """
    all_flags: list[str] = []
    all_warnings: list[str] = []

    all_flags += _check_lexical(text)
    all_flags += _check_structural(text)
    all_flags += _check_positive_markers(text)

    brand_flags, brand_warnings = _check_brand_mention(text, brand_name)
    all_flags += brand_flags
    all_warnings += brand_warnings

    # Title AI-tell check (em dash / banned phrase / explainer cliché)
    all_flags += _check_title(title)

    # Title restatement check
    if title:
        body = re.sub(r'^#{1,6}[^\n]*\n', '', text, flags=re.MULTILINE).strip()
        sentences = _split_sentences(body)
        first_sentence = sentences[0] if sentences else ""
        STOP = {'the', 'a', 'an', 'of', 'in', 'and', 'to', 'for', 'is', 'its', 'your',
                'our', 'their', 'what', 'how', 'why', 'when', 'where'}
        title_words = set(title.lower().split()) - STOP
        first_sentence_words = set(first_sentence.lower().split()) - STOP
        overlap = title_words & first_sentence_words
        if title_words and len(overlap) / len(title_words) > 0.6:
            all_flags.append(
                f"Opening sentence appears to restate the title "
                f"({len(overlap)} of {len(title_words)} key title words overlap)"
            )

    # Date grounding check (requires brief)
    if brief is not None:
        all_warnings += _check_date_grounding(text, brief)

    return GateResult(
        passed=len(all_flags) == 0,
        flags=all_flags,
        warnings=all_warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI usage: python -m pipeline.compliance_gate path/to/article.md
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run compliance gate on an article file.")
    parser.add_argument("file", help="Path to the article file (markdown or .txt)")
    parser.add_argument("--title", default="", help="Article title (for restatement check)")
    parser.add_argument("--brief", default="", help="Path to brief JSON (for date grounding check)")
    parser.add_argument("--brand", default="Cashbucket", help="Brand name (for mention check)")
    args = parser.parse_args()

    article_text = Path(args.file).read_text(encoding="utf-8")
    brief_data = json.loads(Path(args.brief).read_text(encoding="utf-8")) if args.brief else None
    result = check(article_text, title=args.title, brief=brief_data, brand_name=args.brand)

    print(result.summary())
    sys.exit(0 if result.passed else 1)
