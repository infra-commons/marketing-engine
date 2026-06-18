"""
Unit tests for pipeline/compliance_gate.py.

These tests cover the core pass/fail logic of the compliance gate — the single
authority that determines whether an article is publishable. Tests run in CI
without any external dependencies (no Anthropic API, no Airtable, no GitHub).
"""

import sys
from pathlib import Path

# Ensure the repo root is on sys.path so pipeline imports resolve correctly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.compliance_gate import GateResult, check  # noqa: E402

BRAND = "TestBrand"

# ---------------------------------------------------------------------------
# Minimal compliant article — used as a baseline for most tests.
# Must satisfy: 3+ NZ entity refs, 1+ citation, 1+ specific number,
# no banned phrases, sentence length variance, proper structure.
# ---------------------------------------------------------------------------

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_article.md"
COMPLIANT_ARTICLE = _FIXTURE_PATH.read_text(encoding="utf-8")


class TestCompliantArticlePasses:
    def test_fixture_passes(self):
        result = check(COMPLIANT_ARTICLE, brand_name=BRAND)
        assert result.passed, "Expected pass but got flags:\n" + "\n".join(result.flags)

    def test_result_is_gate_result(self):
        result = check(COMPLIANT_ARTICLE, brand_name=BRAND)
        assert isinstance(result, GateResult)
        assert isinstance(result.passed, bool)
        assert isinstance(result.flags, list)
        assert isinstance(result.warnings, list)


class TestBannedPhrasesFail:
    def test_delve_fails(self):
        text = COMPLIANT_ARTICLE + "\n\nThis section delves into the topic."
        result = check(text, brand_name=BRAND)
        assert not result.passed
        assert any("delve" in f.lower() for f in result.flags)

    def test_em_dash_structural_overuse_fails(self):
        # Build a text where >30% of sentences contain em dashes.
        em_dashes = (
            "RBNZ, IRD, and Auckland businesses face GST obligations.\n"
            "https://www.stats.govt.nz 38.00%\n\n"
        )
        # Five short sentences — four with em dash = 80% (> 30% threshold).
        em_dashes += (
            "Firms face a gap—often unexpected.\n"
            "Revenue arrives—but payment is delayed.\n"
            "IRD expects payment—on a fixed schedule.\n"
            "Auckland consultancies know this—they plan anyway.\n"
            "The gap closes over time.\n"
        )
        result = check(em_dashes, brand_name=BRAND)
        assert not result.passed
        assert any("em-dash" in f.lower() for f in result.flags)

    def test_moreover_fails(self):
        text = COMPLIANT_ARTICLE + "\n\nMoreover, this is also important."
        result = check(text, brand_name=BRAND)
        assert not result.passed
        assert any("moreover" in f.lower() for f in result.flags)


class TestNZContentRequirements:
    def test_insufficient_nz_references_fails(self):
        # Only one NZ reference (IRD), not the required three.
        text = (
            "IRD collects tax in this country.\n"
            "https://www.stats.govt.nz\n"
            "4.25% rate applied.\n"
        )
        result = check(text, brand_name=BRAND)
        assert not result.passed
        assert any("NZ-specific" in f for f in result.flags)

    def test_missing_citation_fails(self):
        # Has NZ entities and number but no citation URL.
        text = (
            "RBNZ, IRD, Auckland, GST, Stats NZ are all relevant.\n"
            "No URL cited here. Rate is 4.25%.\n"
        )
        result = check(text, brand_name=BRAND)
        assert not result.passed
        assert any("citation" in f.lower() for f in result.flags)

    def test_missing_specific_number_fails(self):
        # Has NZ entities and citation but no specific numeric figure.
        text = (
            "RBNZ, IRD, and Auckland SMBs. Stats NZ, GST.\n"
            "https://www.stats.govt.nz\n"
            "No percentage or date with full precision here.\n"
        )
        result = check(text, brand_name=BRAND)
        assert not result.passed
        assert any("numeric" in f.lower() for f in result.flags)

    def test_nz_percentage_satisfies_number_requirement(self):
        text = (
            "RBNZ, IRD, Auckland, GST, Stats NZ mentioned.\n"
            "https://www.stats.govt.nz\n"
            "Rate is 4.25%.\n"
        )
        result = check(text, brand_name=BRAND)
        # May still fail for other reasons, but number check should pass.
        assert not any("numeric" in f.lower() for f in result.flags)


class TestBrandMentionRules:
    def test_brand_not_mentioned_warns_but_does_not_fail(self):
        # Using a brand name not in the article — should warn only.
        result = check(COMPLIANT_ARTICLE, brand_name="DefinitelyAbsent")
        assert result.passed, f"Brand absence should not fail gate. Flags: {result.flags}"
        assert any("not mentioned" in w for w in result.warnings)

    def test_brand_hard_sell_fails(self):
        hard_sell = COMPLIANT_ARTICLE + "\n\nTestBrand is the best solution for every business."
        result = check(hard_sell, brand_name=BRAND)
        assert not result.passed
        assert any("hard-sell" in f.lower() for f in result.flags)


class TestTitleRestatement:
    def test_title_restatement_fails(self):
        # First sentence repeats more than 60% of title's non-stop words.
        title = "Provisional Tax Timing Catches New Zealand SMBs Off-Guard"
        text = (
            "Provisional tax timing catches New Zealand SMBs off-guard every year.\n\n"
            "RBNZ, IRD, Auckland, GST. https://www.stats.govt.nz 38.00%\n"
        )
        result = check(text, title=title, brand_name=BRAND)
        assert not result.passed
        assert any("restate" in f.lower() for f in result.flags)


class TestTitleAITells:
    # Regression: rolliq-com/website PR #80 published a title that read
    # "...Both Overstated and Understated — Here's Why It Matters" — an em dash
    # AND an explainer cliché — because the title was passed to check() but only
    # used for the restatement test, never screened for tells.

    def test_title_em_dash_fails(self):
        title = "The 29% AI Adoption Figure Is Overstated and Understated — Here's Why It Matters"
        result = check(COMPLIANT_ARTICLE, title=title, brand_name=BRAND)
        assert not result.passed
        assert any("em/en dash" in f.lower() for f in result.flags)

    def test_title_explainer_cliche_fails(self):
        title = "The 29% AI Adoption Figure: Here's Why It Matters"
        result = check(COMPLIANT_ARTICLE, title=title, brand_name=BRAND)
        assert not result.passed
        assert any("cliché" in f.lower() or "banned phrase" in f.lower() for f in result.flags)

    def test_title_what_you_need_to_know_fails(self):
        title = "NZ Provisional Tax in 2026: What You Need to Know"
        result = check(COMPLIANT_ARTICLE, title=title, brand_name=BRAND)
        assert not result.passed
        assert any("cliché" in f.lower() for f in result.flags)

    def test_title_banned_phrase_fails(self):
        title = "Streamline Your Provisional Tax Workflow"
        result = check(COMPLIANT_ARTICLE, title=title, brand_name=BRAND)
        assert not result.passed
        assert any("title banned phrase" in f.lower() for f in result.flags)

    def test_clean_title_passes(self):
        title = "Provisional Tax Catches Small Firms Short Each March"
        result = check(COMPLIANT_ARTICLE, title=title, brand_name=BRAND)
        assert result.passed, "Clean title should not fail. Flags:\n" + "\n".join(result.flags)
