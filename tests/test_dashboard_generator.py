"""
Unit tests for pipeline/dashboard_generator.py.

These cover the brand-driven template behaviour that matters for the shared
engine: article items render as real links (live + GitHub source), and the
rendered CSS is themed from the brand's `colors` dict rather than any hardcoded
brand palette. All tests are pure rendering — no network, no secrets.
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.dashboard_generator import (  # noqa: E402
    _approve_list,
    _draft_summary,
    _parse_token_expiry,
    _render_html,
    _slug_list,
    _theme_vars,
    _token_warnings,
)

# A deliberately non-cashbucket palette so we can assert the template themes
# from brand config rather than any baked-in colour.
FIXTURE_COLORS = {
    "primary": "#aa00ff",
    "primary_dark": "#7700bb",
    "accent": "#ff8800",
    "heading": "#222222",
    "text": "#333333",
    "gray_bg": "#fafafa",
}

RENDER_KWARGS = dict(
    generated_at="2026-06-21 06:00",
    published_count=1,
    approved_count=0,
    queued_count=1,
    last_published={"slug": "my-published-article", "published_at": "2026-06-01"},
    next_approved=[],
    next_queued=[{"slug": "my-queued-article", "draft_path": "staging/drafts/draft-009-v1.md"}],
    stalled=[],
    blocked_count=0,
    draft_count=5,
    social={"linkedin": 2, "x": 1},
    buffer_sent_count=0,
    buffer_pending=[],
    tokens=[],
    brand_slug="acme",
    colors=FIXTURE_COLORS,
    title="Acme Dashboard",
    refresh_url="https://github.com/acme-com/marketing/actions/workflows/deploy-dashboard.yml",
    staging_site_url="https://acme-staging.example.dev",
    article_url_base="https://acme.example/articles",
    marketing_repo="acme-com/marketing",
)


class TestTheming:
    def test_theme_vars_uses_brand_primary(self):
        css = _theme_vars(FIXTURE_COLORS)
        assert "--primary: #aa00ff" in css
        assert "--accent: #ff8800" in css

    def test_render_themes_from_brand_colors(self):
        html = _render_html(**RENDER_KWARGS)
        assert "--primary: #aa00ff" in html
        # No cashbucket teal leaks into a non-cashbucket render.
        assert "#059FAF" not in html
        # CSS routes through the variable, not literal brand hex.
        assert "var(--primary)" in html

    def test_title_is_brand_driven(self):
        html = _render_html(**RENDER_KWARGS)
        assert "<title>Acme Dashboard</title>" in html
        assert "<h1>Acme Dashboard</h1>" in html


class TestArticleLinks:
    def test_last_published_links_to_live_url(self):
        html = _render_html(**RENDER_KWARGS)
        assert 'href="https://acme.example/articles/my-published-article"' in html

    def test_queued_item_links_to_github_source(self):
        html = _render_html(**RENDER_KWARGS)
        assert (
            'href="https://github.com/acme-com/marketing/blob/main/'
            'brands/acme/staging/drafts/draft-009-v1.md"' in html
        )

    def test_drafts_card_links_to_github_dir(self):
        html = _render_html(**RENDER_KWARGS)
        assert (
            'href="https://github.com/acme-com/marketing/tree/main/'
            'brands/acme/staging/drafts"' in html
        )

    def test_approve_list_builds_github_blob_link(self):
        html = _approve_list(
            [{"slug": "foo", "draft_path": "staging/drafts/draft-001-v1.md"}],
            staging_site_url="https://staging.example",
            marketing_repo="acme-com/marketing",
            brand_slug="acme",
        )
        assert (
            'href="https://github.com/acme-com/marketing/blob/main/'
            'brands/acme/staging/drafts/draft-001-v1.md"' in html
        )
        # Preview + Approve buttons are preserved alongside the link.
        assert 'class="stage-btn"' in html
        assert 'class="approve-btn"' in html

    def test_approve_list_degrades_without_marketing_repo(self):
        # No marketing_repo => plain slug text, no broken link.
        html = _approve_list(
            [{"slug": "foo", "draft_path": "staging/drafts/draft-001-v1.md"}],
            staging_site_url="https://staging.example",
            marketing_repo="",
            brand_slug="acme",
        )
        assert "<a href=" not in html
        assert "foo" in html


class TestTokenExpiry:
    def test_parse_accepts_strings_and_dates(self):
        parsed = _parse_token_expiry({"a": "2026-08-30", "b": date(2026, 9, 1), "bad": "nope"})
        assert parsed == {"a": date(2026, 8, 30), "b": date(2026, 9, 1)}

    def test_warnings_classify_by_days(self):
        today = date(2026, 6, 21)
        tokens = _token_warnings(today, {
            "soon": date(2026, 6, 25),     # 4 days -> danger
            "mid": date(2026, 7, 21),      # 30 days -> warning
            "far": date(2026, 12, 21),     # ~183 days -> ok
        })
        by_name = {t["name"]: t["status"] for t in tokens}
        assert by_name == {"soon": "danger", "mid": "warning", "far": "ok"}
        # Sorted soonest-first.
        assert [t["name"] for t in tokens] == ["soon", "mid", "far"]


class TestApproveRowContent:
    """The approve row has to carry enough of the article to approve it on.

    A slug and two buttons give the approver nothing to base a decision on, and leave the
    recorded verdict -- the one `queue_manager approve` refuses on -- invisible.
    """

    APPROVE_KWARGS = dict(
        staging_site_url="https://staging.example",
        marketing_repo="acme-com/marketing",
        brand_slug="acme",
    )

    def _row(self, **overrides):
        item = {
            "slug": "foo",
            "draft_path": "staging/drafts/draft-001-v1.md",
            "title": "What The Wage Change Actually Costs",
            "word_count": 1240,
            "excerpt": "The headline rate is the smallest part of the bill.",
            "gate_passed": True,
            "dates_verified": True,
        }
        item.update(overrides)
        return _approve_list([item], **self.APPROVE_KWARGS)

    def test_row_shows_title_word_count_and_excerpt(self):
        html = self._row()
        assert "What The Wage Change Actually Costs" in html
        assert "1,240 words" in html
        assert "The headline rate is the smallest part of the bill." in html

    def test_clean_entry_shows_no_verdict_warning(self):
        html = self._row()
        assert "approve-verdict" not in html
        assert "approve-blocker" not in html

    def test_failed_gate_is_named_with_its_reason(self):
        html = self._row(gate_passed=False, gate_flags=["banned phrase"])
        assert "GATE" in html
        assert "failed the compliance gate" in html
        assert "banned phrase" in html

    def test_unverified_brief_is_named(self):
        html = self._row(dates_verified=False)
        assert "DATES" in html
        assert "primary source" in html

    def test_requeued_entry_shows_its_stale_verdict(self):
        html = self._row(gate_stale=True)
        assert "STALE" in html
        assert "re-gated" in html

    def test_entry_with_no_recorded_verdict_reads_as_ungated(self):
        # Absence is not a pass. An entry that predates gate recording, or was added by
        # something that never ran the gate, must not render as a clean one.
        html = _approve_list(
            [{"slug": "foo", "draft_path": "staging/drafts/draft-001-v1.md"}],
            **self.APPROVE_KWARGS,
        )
        assert "UNGATED" in html

    def test_row_content_is_html_escaped(self):
        html = self._row(title="Wages <script>alert(1)</script> & levies")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp; levies" in html

    def test_buttons_stay_enabled_on_a_blocked_entry(self):
        # Rendering the reason is this module's job; disabling a control owned by the
        # consuming repo's /api/approve endpoint is not.
        html = self._row(gate_passed=False)
        assert "disabled" not in html
        assert 'class="approve-btn"' in html


class TestDraftSummary:
    DRAFT = "# A Real Headline\n\nThe opening paragraph of the article body.\n\nMore prose here.\n"

    def test_reads_title_word_count_and_excerpt_from_the_draft(self, tmp_path):
        (tmp_path / "staging" / "drafts").mkdir(parents=True)
        (tmp_path / "staging" / "drafts" / "d.md").write_text(self.DRAFT, encoding="utf-8")
        summary = _draft_summary(tmp_path, {"draft_path": "staging/drafts/d.md"})
        assert summary["title"] == "A Real Headline"
        assert summary["word_count"] == len(
            "The opening paragraph of the article body.\n\nMore prose here.".split()
        )
        assert "opening paragraph" in summary["excerpt"]

    def test_recorded_word_count_wins_over_a_recount(self, tmp_path):
        (tmp_path / "d.md").write_text(self.DRAFT, encoding="utf-8")
        summary = _draft_summary(tmp_path, {"draft_path": "d.md", "word_count": 999})
        assert summary["word_count"] == 999

    def test_recorded_description_is_preferred_as_the_excerpt(self, tmp_path):
        (tmp_path / "d.md").write_text(self.DRAFT, encoding="utf-8")
        summary = _draft_summary(tmp_path, {"draft_path": "d.md", "description": "Recorded."})
        assert summary["excerpt"] == "Recorded."

    def test_missing_draft_renders_a_thinner_row_rather_than_raising(self, tmp_path):
        # draft_path goes stale legitimately under the dir-move workflow. A dashboard that
        # fails to render tells the approver less than one that renders with a gap.
        summary = _draft_summary(tmp_path, {"draft_path": "staging/drafts/gone.md"})
        assert summary == {"title": "", "word_count": None, "excerpt": ""}

    def test_entry_with_no_draft_path_is_handled(self, tmp_path):
        assert _draft_summary(tmp_path, {})["word_count"] is None


class TestStalledCard:
    """`hold` and `pending_review` appear in no other card, so a queue stalled on one
    of them rendered as a queue with nothing in it."""

    def _html(self, **overrides):
        kwargs = dict(RENDER_KWARGS)
        kwargs.update(overrides)
        return _render_html(**kwargs)

    def test_pending_review_entry_is_counted_and_named(self):
        html = self._html(stalled=[{"slug": "stranded-article"}])
        assert "Stalled" in html
        assert "stranded-article" in html

    def test_stalled_card_offers_the_transition_out(self):
        # `requeue` is the only exit from these statuses. A card that reports the state
        # without naming the remedy leaves the operator where the missing transition did.
        html = self._html(stalled=[{"slug": "stranded-article"}])
        assert "queue_manager --brand acme requeue" in html

    def test_empty_queue_renders_the_zero_state(self):
        html = self._html(stalled=[], blocked_count=0)
        assert "Nothing held or awaiting review" in html
        assert "requeue" not in html

    def test_blocked_entries_are_reported_but_not_added_to_the_count(self):
        # A blocked entry is normally also `queued`, so counting it here would put the
        # same article on two cards.
        html = self._html(stalled=[], blocked_count=2)
        assert "2 queued entries blocked on a recorded verdict" in html
        assert "Nothing held or awaiting review" in html

    def test_a_single_blocked_entry_reads_as_singular(self):
        assert "1 queued entry blocked" in self._html(stalled=[], blocked_count=1)


class TestRecordedGateReasons:
    """The gate's reasons reach the row, not just its verdict.

    `gate_flags` had a reader (`queue_policy.recorded_blockers` names them in the sentence
    it renders) and a clearer (`requeue`) before it had a writer, so the parenthesis was
    always empty in practice: a failed entry told its approver that it failed and never
    why. `gate_warnings` had no reader at all.
    """

    KWARGS = TestApproveRowContent.APPROVE_KWARGS

    def _row(self, **overrides):
        item = {
            "slug": "foo",
            "draft_path": "staging/drafts/draft-001-v1.md",
            "title": "What The Wage Change Actually Costs",
            "word_count": 1240,
            "excerpt": "The headline rate is the smallest part of the bill.",
            "gate_passed": True,
            "gate_flags": [],
            "gate_warnings": [],
            "dates_verified": True,
        }
        item.update(overrides)
        return _approve_list([item], **self.KWARGS)

    def test_recorded_flags_are_named_in_the_blocker_sentence(self):
        html = self._row(gate_passed=False, gate_flags=["em dash in headline", "banned phrase"])
        assert "failed the compliance gate" in html
        assert "em dash in headline" in html
        assert "banned phrase" in html

    def test_advisory_warnings_are_rendered(self):
        html = self._row(gate_warnings=["a 2026 figure does not appear in the brief"])
        assert "a 2026 figure does not appear in the brief" in html
        assert "approve-warning" in html

    def test_a_warning_does_not_read_as_a_blocker(self):
        # An advisory does not refuse a publish. Styling the two alike would either inflate
        # a warning into a block or deflate a block into a note.
        html = self._row(gate_warnings=["an advisory"])
        assert "approve-blocker" not in html
        assert "an advisory" in html

    def test_a_blocked_entry_can_carry_warnings_too(self):
        html = self._row(gate_passed=False, gate_flags=["banned phrase"], gate_warnings=["an advisory"])
        assert "approve-blocker" in html
        assert "approve-warning" in html

    def test_warnings_are_html_escaped(self):
        html = self._row(gate_warnings=["<script>alert(1)</script>"])
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_gated_entry_with_no_reasons_shows_neither(self):
        # Empty lists are a positive statement that the gate ran and found nothing.
        html = self._row()
        assert "approve-blocker" not in html
        assert "approve-warning" not in html
        assert "approve-verdict" not in html


class TestNothingWaitingIsHidden:
    """The card that exists to show what is waiting shows all of it.

    It was capped at three while the count above it reported the true total, so a fourth
    waiting article was invisible and the card disagreed with its own headline.
    """

    def _items(self, n):
        return [{"slug": f"article-{i}", "draft_path": f"staging/drafts/draft-{i}-v1.md"} for i in range(n)]

    def test_a_fourth_queued_article_is_rendered(self):
        html = _approve_list(self._items(4), **TestApproveRowContent.APPROVE_KWARGS)
        for i in range(4):
            assert f"article-{i}" in html
        assert html.count('class="approve-btn"') == 4

    def test_a_much_longer_queue_is_rendered_in_full(self):
        html = _approve_list(self._items(12), **TestApproveRowContent.APPROVE_KWARGS)
        assert html.count('class="approve-btn"') == 12

    def test_an_explicit_limit_is_still_honoured(self):
        html = _approve_list(self._items(4), limit=2, **TestApproveRowContent.APPROVE_KWARGS)
        assert html.count('class="approve-btn"') == 2


class TestSlugListElision:
    """The compact lists keep their cap but stop hiding it.

    A list silently shorter than the number printed above it is two statements about the
    same set that disagree, with no way to tell which is wrong.
    """

    def _items(self, n):
        return [{"slug": f"article-{i}"} for i in range(n)]

    def test_an_elided_list_says_how_many_it_dropped(self):
        assert "+2 more" in _slug_list(self._items(5))

    def test_a_list_within_the_cap_says_nothing(self):
        assert "more" not in _slug_list(self._items(3))

    def test_a_list_exactly_one_over_names_the_one(self):
        assert "+1 more" in _slug_list(self._items(4))

    def test_the_marker_is_not_rendered_as_a_slug(self):
        assert 'class="slug-more"' in _slug_list(self._items(5))

    def test_an_empty_list_still_renders_the_dash(self):
        assert _slug_list([]) == "<p>—</p>"
