"""
Content Engine — Signal Scrubber

Runs BEFORE the compliance gate. Detects AI tells with line-level detail
so they can be fixed before hitting the hard-fail gate.

The gate tells you WHAT failed. The scrubber tells you WHERE.

Does not modify the source file.
Exit 0 = clean (or advisory warnings only). Exit 1 = issues found.

Usage:
    python3 -m pipeline.signal_scrubber path/to/draft.md
    python3 -m pipeline.signal_scrubber path/to/draft.md --brand rolliq
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from banned_phrases import (
    BANNED_PHRASES, EM_DASH_CHARS,
    LEVERAGE_SYNONYMS, DELVE_SYNONYMS, UNLOCK_SYNONYMS,
)

ALL_BANNED = BANNED_PHRASES + LEVERAGE_SYNONYMS + DELVE_SYNONYMS + UNLOCK_SYNONYMS

# Simple substitution hints for the most common banned phrases.
# Not exhaustive — just the ones that appear most in AI output.
_SUBSTITUTIONS: dict[str, str] = {
    "leverage":               "use / apply / run",
    "leveraging":             "using / applying",
    "capitalise on":          "use / take advantage of",
    "capitalize on":          "use / take advantage of",
    "harness the power":      "use",
    "delve":                  "look at / examine / go through",
    "delving":                "looking at / examining",
    "unpack this":            "break this down",
    "unlock":                 "open up / create / enable",
    "unleash potential":      "create value",
    "seamless":               "remove or rephrase",
    "seamlessly":             "remove or rephrase",
    "robust":                 "solid / reliable / practical",
    "in today's fast-paced":  "remove — cut to the actual point",
    "moreover,":              "remove — start a new sentence",
    "furthermore,":           "remove — start a new sentence",
    "additionally,":          "remove — start a new sentence",
    "game-changer":           "rephrase with a specific claim",
    "game changer":           "rephrase with a specific claim",
    "transformative":         "rephrase with a specific outcome",
    "groundbreaking":         "rephrase with a specific claim",
    "cutting-edge":           "name the specific technology",
    "state-of-the-art":       "name the specific technology",
}


@dataclass
class ScrubIssue:
    kind: str       # "banned_phrase", "em_dash", "sentence_band", "parallel_openers", "heading_density"
    severity: str   # "hard" (gate will fail) or "advisory"
    message: str
    lines: list[int] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)


@dataclass
class ScrubReport:
    source: str
    issues: list[ScrubIssue] = field(default_factory=list)

    @property
    def hard_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "hard")

    @property
    def advisory_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "advisory")

    def summary(self) -> str:
        lines = [f"SIGNAL SCRUB: {self.source}", "═" * 50]

        if not self.issues:
            lines.append("\nNo issues found. Safe to run compliance gate.")
            return "\n".join(lines)

        hard = [i for i in self.issues if i.severity == "hard"]
        advisory = [i for i in self.issues if i.severity == "advisory"]

        if hard:
            lines.append(f"\nHARD ISSUES — gate will fail ({len(hard)})\n")
            for issue in hard:
                lines.append(f"  [{issue.kind}] {issue.message}")
                for ln, ex in zip(issue.lines, issue.excerpts):
                    lines.append(f"    line {ln}: {ex}")
                lines.append("")

        if advisory:
            lines.append(f"\nADVISORY — review before publishing ({len(advisory)})\n")
            for issue in advisory:
                lines.append(f"  [{issue.kind}] {issue.message}")
                for ln, ex in zip(issue.lines, issue.excerpts):
                    lines.append(f"    line {ln}: {ex}")
                lines.append("")

        lines.append("─" * 50)
        parts = []
        if hard:
            parts.append(f"{len(hard)} hard issue(s)")
        if advisory:
            parts.append(f"{len(advisory)} advisory")
        lines.append(", ".join(parts) + " — fix hard issues before running compliance gate.")

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Detectors
# ─────────────────────────────────────────────────────────────────────────────

def _detect_banned_phrases(lines: list[str]) -> list[ScrubIssue]:
    issues = []
    for lineno, line in enumerate(lines, start=1):
        lower = line.lower()
        for phrase in ALL_BANNED:
            if phrase in lower:
                hint = _SUBSTITUTIONS.get(phrase, "rephrase")
                excerpt = line.strip()
                if len(excerpt) > 80:
                    idx = lower.find(phrase)
                    excerpt = "…" + line[max(0, idx - 20):idx + len(phrase) + 30].strip() + "…"
                issues.append(ScrubIssue(
                    kind="banned_phrase",
                    severity="hard",
                    message=f'"{phrase}" → {hint}',
                    lines=[lineno],
                    excerpts=[excerpt],
                ))
    return issues


def _detect_em_dash_overuse(lines: list[str]) -> list[ScrubIssue]:
    text = "\n".join(lines)
    clean = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    clean = re.sub(r'^\s*[-*]\s+', '', clean, flags=re.MULTILINE)
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', clean) if len(s.strip()) > 10]

    if not sentences:
        return []

    offending = [s for s in sentences if any(c in s for c in EM_DASH_CHARS) or ' -- ' in s]
    pct = len(offending) / len(sentences)

    if pct <= 0.30:
        return []

    # Map offending sentences back to line numbers (best-effort)
    located: list[tuple[int, str]] = []
    for sentence in offending[:6]:
        needle = sentence[:40].strip()
        for lineno, line in enumerate(lines, start=1):
            if needle in line:
                located.append((lineno, sentence[:90] + ("…" if len(sentence) > 90 else "")))
                break
        else:
            located.append((0, sentence[:90] + ("…" if len(sentence) > 90 else "")))

    issue_lines = [ln for ln, _ in located]
    excerpts = [ex for _, ex in located]
    if len(offending) > 6:
        excerpts[-1] += f" (+ {len(offending) - 6} more)"

    return [ScrubIssue(
        kind="em_dash",
        severity="hard",
        message=f"Em dash in {pct:.0%} of sentences (limit 30%) — replace em dashes with colons, full stops, or commas",
        lines=issue_lines,
        excerpts=excerpts,
    )]


def _detect_sentence_band(lines: list[str]) -> list[ScrubIssue]:
    text = "\n".join(lines)
    clean = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    clean = re.sub(r'^\s*[-*]\s+', '', clean, flags=re.MULTILINE)
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', clean) if len(s.strip()) > 10]
    word_counts = [len(s.split()) for s in sentences if len(s.split()) > 2]

    if not word_counts:
        return []

    in_band = sum(1 for wc in word_counts if 12 <= wc <= 22)
    pct = in_band / len(word_counts)

    if pct <= 0.50:
        return []

    return [ScrubIssue(
        kind="sentence_band",
        severity="hard",
        message=(
            f"{pct:.0%} of sentences are 12–22 words (limit 50%) — "
            "add more variation: short punchy sentences and longer complex ones"
        ),
    )]


def _detect_parallel_openers(lines: list[str]) -> list[ScrubIssue]:
    text = "\n".join(lines)
    paragraphs_raw = [p.strip() for p in text.split('\n\n') if p.strip()]

    def first_word(para: str) -> str:
        clean = re.sub(r'^#{1,6}\s+', '', para)
        words = clean.strip().split()
        return words[0].lower().rstrip('.,;:') if words else ''

    issues = []
    for i in range(len(paragraphs_raw) - 2):
        group = [paragraphs_raw[i + j] for j in range(3)]
        words = [first_word(p) for p in group]
        if len(set(words)) == 1 and words[0]:
            # Find the line number of the first paragraph in the run
            needle = group[0][:40]
            found_line = 0
            for lineno, line in enumerate(lines, start=1):
                if line.strip() and line.strip() in needle:
                    found_line = lineno
                    break
            issues.append(ScrubIssue(
                kind="parallel_openers",
                severity="hard",
                message=f'3+ consecutive paragraphs start with "{words[0]}" — structural AI tell',
                lines=[found_line] if found_line else [],
                excerpts=[p[:70] + "…" for p in group[:3]],
            ))
            break  # one report is enough; gate catches all instances

    return issues


def _detect_heading_density(lines: list[str]) -> list[ScrubIssue]:
    text = "\n".join(lines)
    word_count = len(text.split())
    heading_count = len(re.findall(r'^#{1,4}\s+\S', text, re.MULTILINE))

    if heading_count == 0 or word_count / heading_count >= 100:
        return []

    return [ScrubIssue(
        kind="heading_density",
        severity="hard",
        message=(
            f"1 heading per {word_count // heading_count} words (minimum 100) — "
            "consolidate sub-sections or remove headings"
        ),
    )]


def _detect_summary_closer(lines: list[str]) -> list[ScrubIssue]:
    text = "\n".join(lines)
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    if not paragraphs:
        return []

    last = paragraphs[-1].lower().lstrip('#').strip()
    openers = ('in conclusion', 'to conclude', 'in summary', 'to summarise',
               'to summarize', 'as we have seen', 'as outlined above')
    for opener in openers:
        if last.startswith(opener):
            return [ScrubIssue(
                kind="summary_closer",
                severity="hard",
                message=f'Final paragraph opens with "{opener}" — AI summary tell; end on a specific observation',
                lines=[len(lines)],
                excerpts=[paragraphs[-1][:80] + "…"],
            )]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def scrub(text: str, source: str = "") -> ScrubReport:
    """
    Run all signal checks. Returns ScrubReport.

    Args:
        text:   Full article text (markdown or plain text).
        source: Filename or identifier for the report header.
    """
    lines = text.splitlines()
    report = ScrubReport(source=source or "article")

    report.issues += _detect_banned_phrases(lines)
    report.issues += _detect_em_dash_overuse(lines)
    report.issues += _detect_sentence_band(lines)
    report.issues += _detect_parallel_openers(lines)
    report.issues += _detect_heading_density(lines)
    report.issues += _detect_summary_closer(lines)

    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Detect AI signals in a draft before running the compliance gate.")
    parser.add_argument("file", help="Path to the article file (markdown or .txt)")
    args = parser.parse_args()

    path = Path(args.file)
    text = path.read_text(encoding="utf-8")
    report = scrub(text, source=path.name)

    print(report.summary())
    sys.exit(0 if report.hard_count == 0 else 1)
