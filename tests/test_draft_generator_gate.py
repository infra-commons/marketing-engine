"""
Tests for the gate seam in pipeline/draft_generator.py.

The generator runs the compliance gate on its own output and records the verdict; the
publisher runs the same gate again at publish time and BLOCKS on it. For those two runs
to mean the same thing they have to score the same object. They did not: the publisher
passed the H1 it parsed out of the draft, while the generator passed the brief's
`topic_statement` — a paragraph of prose from the topic selector, scored under
`_check_title`, whose rules are written for a headline.

The rule that made this visible is the em-dash check. An em dash in a headline is an AI
tell; in body prose it is ordinary punctuation, which is why the check is applied only to
the title. Any brief whose topic statement contained one therefore failed the gate however
clean the article was, and the retry that followed could not help — `_build_retry_prompt`
returns the flags to the model and asks it to fix a string it never wrote and cannot see,
so attempt 2 fails identically at the cost of a second flagship call.

These tests pin the outcome, not the bug: an article with a clean H1 passes even when the
brief's topic statement would not, and one generation is enough.

`draft_generator` had no test file before this one — it is one of the modules the CI
coverage report shows at 0%.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

# Ensure the repo root is on sys.path so pipeline imports resolve correctly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import draft_generator  # noqa: E402
from pipeline.compliance_gate import check as gate_check  # noqa: E402
from pipeline.publisher import parse_draft, parse_draft_text  # noqa: E402

BRAND = "Test Brand"

# The gate-compliant article the compliance_gate suite already asserts against. Its H1
# carries no em dash, so it is the "healthy case" the old code failed.
CLEAN_ARTICLE = (Path(__file__).parent / "fixtures" / "sample_article.md").read_text(
    encoding="utf-8"
)

CLEAN_H1 = "# Provisional Tax Timing Catches New Zealand SMBs Off-Guard"
EM_DASHED_H1 = "# Provisional Tax Timing — What It Costs New Zealand SMBs"

# A topic statement in the shape topic_selector emits: prose, and an em dash used as
# ordinary punctuation.
EM_DASHED_TOPIC_STATEMENT = (
    "Provisional tax instalment dates are fixed by IRD rather than by when a business "
    "actually holds cash — leaving smaller firms to absorb the gap themselves."
)


def _with_em_dashed_headline(article: str) -> str:
    return article.replace(CLEAN_H1, EM_DASHED_H1, 1)


# ─────────────────────────────────────────────────────────────────────────────
# One parse, not two
# ─────────────────────────────────────────────────────────────────────────────

class TestParseDraftText:
    """`parse_draft_text` is the text-level half of `parse_draft`, not a second copy.

    A second implementation would be free to drift, and the drift would show up as the
    generator and the publisher disagreeing about a draft again — the failure this whole
    file is about.
    """

    def test_matches_parse_draft_on_the_same_content(self, tmp_path):
        path = tmp_path / "draft-001-v1.md"
        path.write_text(CLEAN_ARTICLE, encoding="utf-8")
        assert parse_draft_text(CLEAN_ARTICLE) == parse_draft(path)

    def test_returns_the_h1_without_its_marker(self):
        title, _body = parse_draft_text(CLEAN_ARTICLE)
        assert title == CLEAN_H1[2:]

    def test_body_excludes_the_h1_line(self):
        _title, body = parse_draft_text(CLEAN_ARTICLE)
        assert not body.startswith("# ")

    def test_titleless_text_yields_an_empty_title(self):
        title, body = parse_draft_text("Just a paragraph with no heading.\n")
        assert title == ""
        assert body == "Just a paragraph with no heading."


# ─────────────────────────────────────────────────────────────────────────────
# The gate scores the headline
# ─────────────────────────────────────────────────────────────────────────────

class TestGateScoresTheArticlesOwnHeadline:
    def test_clean_article_passes_despite_an_em_dashed_topic_statement(self):
        brief = {"brief_id": "brief-001", "topic_statement": EM_DASHED_TOPIC_STATEMENT}
        result = draft_generator._gate(
            CLEAN_ARTICLE, brief, SimpleNamespace(display_name=BRAND)
        )
        assert result.passed, "unexpected flags:\n" + "\n".join(result.flags)

    def test_the_topic_statement_is_what_used_to_fail(self):
        """The control. Scoring prose under the headline rules is what produced the flag.

        Kept as an explicit assertion rather than a comment so that if `_check_title`'s
        em-dash rule is ever relaxed, this test says so instead of the fix above quietly
        becoming a no-op that proves nothing.
        """
        result = gate_check(
            CLEAN_ARTICLE, title=EM_DASHED_TOPIC_STATEMENT, brand_name=BRAND
        )
        assert not result.passed
        assert any("em/en dash" in flag for flag in result.flags)

    def test_an_em_dashed_headline_still_fails(self):
        """The rule itself is not weakened — only the string it is applied to changed."""
        result = draft_generator._gate(
            _with_em_dashed_headline(CLEAN_ARTICLE),
            {},
            SimpleNamespace(display_name=BRAND),
        )
        assert not result.passed
        assert any("em/en dash" in flag for flag in result.flags)

    def test_it_gates_the_same_title_the_publisher_will(self):
        """The property that matters: one article, one verdict, at both stages."""
        brief = {"brief_id": "brief-001", "topic_statement": EM_DASHED_TOPIC_STATEMENT}
        generator_result = draft_generator._gate(
            CLEAN_ARTICLE, brief, SimpleNamespace(display_name=BRAND)
        )
        # What publisher.py does at its own gate step: parse the H1, gate against it.
        title, body = parse_draft_text(CLEAN_ARTICLE)
        publisher_result = gate_check(
            f"# {title}\n\n{body}", title=title, brief=brief, brand_name=BRAND
        )
        assert generator_result.passed == publisher_result.passed
        assert generator_result.flags == publisher_result.flags


# ─────────────────────────────────────────────────────────────────────────────
# generate() end to end, with the model call stubbed
# ─────────────────────────────────────────────────────────────────────────────

_BRAND_YAML = {
    "brand": "testbrand",
    "display_name": BRAND,
    "site_repo": "test/site",
    "site_local_name": "site",
    "site_url": "https://example.test",
    "gh_token_env": "GH_TOKEN",
    "unsplash_key_env": "UNSPLASH_KEY",
    "articles_path": "articles",
    "articles_index": "articles.html",
    "assets_path": "assets",
    "articles_grid_marker": "<!-- grid -->",
    "article_url_base": "https://example.test/articles",
    "unsplash_utm_source": "testbrand",
    "cta_booking_url": "https://example.test/book",
    "cta_contact_url": "https://example.test/contact",
    "cta_headline": "h",
    "cta_body": "b",
    "cta_btn_primary": "p",
    "cta_btn_secondary": "s",
    "tagline": "t",
    "platform_description": "NZ SMEs",
    "brand_section_label": "Test Brand",
    "colors": {"primary": "#000"},
    "hero_gradients": {"default": "x"},
    "article_type_labels": {"explainer": "Explainer"},
    "social": {},
    "workflow": {"approval": "status", "draft_dir": "drafts"},
}

# The brand's own phrase bank, in the shape brand workspaces use: import the engine base
# and add the brand CTAs. `_select_phrases` reads OPENERS, TRANSITIONS, BRAND_CTAS, CLOSES.
_BRAND_PHRASE_BANKS = """\
from phrase_banks import OPENERS, TRANSITIONS, CLOSES  # noqa: F401

BRAND_CTAS = ["A single brand CTA paragraph."]
"""


class _StubMessages:
    """Stands in for `client.messages`, counting how many generations were asked for."""

    def __init__(self, *articles: str):
        self.articles = list(articles)
        self.calls = []

    def create(self, **kwargs):
        # The last article is reused if the caller asks for more than were supplied, so a
        # stub written for one attempt still answers a retry.
        idx = min(len(self.calls), len(self.articles) - 1)
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(text=self.articles[idx])],
            usage=SimpleNamespace(
                input_tokens=0,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


@pytest.fixture
def stub_brand(tmp_path, monkeypatch):
    """A throwaway consumer repo holding one brand workspace, and no live API client."""
    brand_dir = tmp_path / "brands" / "testbrand"
    (brand_dir / "staging" / "drafts").mkdir(parents=True)
    (brand_dir / "staging" / "briefs").mkdir(parents=True)
    (brand_dir / "brand.yaml").write_text(yaml.safe_dump(_BRAND_YAML), encoding="utf-8")
    (brand_dir / "phrase_banks.py").write_text(_BRAND_PHRASE_BANKS, encoding="utf-8")
    monkeypatch.setenv("MARKETING_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.delenv("CB_ANTHROPIC_API_KEY", raising=False)
    return brand_dir


def _write_brief(brand_dir: Path, topic_statement: str) -> Path:
    brief_path = brand_dir / "staging" / "briefs" / "brief-001.json"
    brief_path.write_text(
        json.dumps(
            {
                "brief_id": "brief-001",
                "article_type": "explainer",
                "topic_statement": topic_statement,
                "key_facts": [],
                "external_citations_required": ["https://www.stats.govt.nz"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return brief_path


def _install_stub(monkeypatch, *articles: str) -> _StubMessages:
    stub = _StubMessages(*articles)
    monkeypatch.setattr(
        draft_generator.anthropic,
        "Anthropic",
        lambda **kwargs: SimpleNamespace(messages=stub),
    )
    return stub


class TestGenerate:
    def test_clean_article_passes_and_costs_one_generation(self, stub_brand, monkeypatch):
        """The regression, end to end.

        Before the fix this brief failed the gate on its topic statement's em dash and
        then spent a second flagship call on a retry that could not possibly clear it.
        """
        stub = _install_stub(monkeypatch, CLEAN_ARTICLE)
        brief_path = _write_brief(stub_brand, EM_DASHED_TOPIC_STATEMENT)

        out_path, result = draft_generator.generate(
            brief_path=str(brief_path),
            brand_slug="testbrand",
            verbose=False,
            dry_run=True,
        )

        assert result.passed, "unexpected flags:\n" + "\n".join(result.flags)
        assert len(stub.calls) == 1, "a passing article must not trigger the retry"
        assert out_path.name == "draft-001-v1.md"

    def test_an_em_dashed_headline_is_retried(self, stub_brand, monkeypatch):
        """The gate is still a gate: a genuine headline AI tell costs the retry."""
        stub = _install_stub(monkeypatch, _with_em_dashed_headline(CLEAN_ARTICLE))
        brief_path = _write_brief(stub_brand, "A clean topic statement with no dash.")

        _out_path, result = draft_generator.generate(
            brief_path=str(brief_path),
            brand_slug="testbrand",
            verbose=False,
            dry_run=True,
        )

        assert not result.passed
        assert any("em/en dash" in flag for flag in result.flags)
        assert len(stub.calls) == 2, "a failing gate must still spend the one retry"

    def test_the_retry_is_gated_on_the_retried_articles_own_headline(
        self, stub_brand, monkeypatch
    ):
        """Attempt 2 may rewrite the H1, so attempt 2's verdict must re-parse it.

        The retry prompt asks for the complete revised article starting with `# [Title]`.
        Carrying attempt 1's title forward would be the same defect in a smaller form:
        a verdict scored against a headline the file does not have.
        """
        stub = _install_stub(
            monkeypatch, _with_em_dashed_headline(CLEAN_ARTICLE), CLEAN_ARTICLE
        )
        brief_path = _write_brief(stub_brand, EM_DASHED_TOPIC_STATEMENT)

        _out_path, result = draft_generator.generate(
            brief_path=str(brief_path),
            brand_slug="testbrand",
            verbose=False,
            dry_run=True,
        )

        assert len(stub.calls) == 2
        assert result.passed, (
            "the retry fixed the headline, so the recorded verdict must reflect the "
            "article that was actually kept:\n" + "\n".join(result.flags)
        )
