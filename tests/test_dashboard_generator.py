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
    _parse_token_expiry,
    _render_html,
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
    held_count=0,
    last_published={"slug": "my-published-article", "published_at": "2026-06-01"},
    next_approved=[],
    next_queued=[{"slug": "my-queued-article", "draft_path": "staging/drafts/draft-009-v1.md"}],
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
