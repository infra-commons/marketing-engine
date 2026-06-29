"""
Unit tests for pipeline/rebake_chrome.py — the re-bake-chrome helper.

Covers the pure rewrite (`rebake_html`) and the directory walk (`rebake_dir`):
the three known header variants converge to the canonical nav, footer
convergence, idempotency, and the multi-block / zero-block skip-and-report path.
Chrome-only is asserted by checking body / hero / publish-date content survives.
Runs in CI with no external dependencies (in-memory + tmp_path only).
"""

import sys
from pathlib import Path

# Ensure the repo root is on sys.path so pipeline imports resolve correctly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.rebake_chrome import rebake_dir, rebake_html  # noqa: E402

# --- Canonical chrome (what every article should converge to) --------------

CANON_NAV = (
    '<header class="site-header"><nav class="nav-inner">'
    '<a class="logo" href="/"><img src="/assets/logo-dark.svg" alt="Rolliq" /></a>'
    '</nav></header>'
)

CANON_FOOTER = (
    '<div class="tagline-strip">AI automation, Wellington NZ.</div>\n'
    '<footer class="site-footer"><div class="footer-inner">'
    '<p>&copy; 2026 Rolliq</p></div></footer>\n'
    '<script>/* burger menu */</script>'
)

# --- Known header variants the rebrand has to fix (vision: rolliq#106) ------

VARIANT_TEXT_WORDMARK = (
    '<header class="site-header"><nav class="nav-inner">'
    '<a class="logo" href="/">Rolliq</a>'
    '</nav></header>'
)

VARIANT_LIGHT_LOGO = (
    '<header class="site-header"><nav class="nav-inner">'
    '<a class="logo" href="/"><img src="/assets/logo-light.svg" alt="Rolliq" /></a>'
    '</nav></header>'
)

OLD_FOOTER = (
    '<div class="tagline-strip">Old tagline.</div>\n'
    '<footer class="site-footer"><div class="footer-inner">'
    '<p>&copy; 2025 Rolliq</p></div></footer>\n'
    '<script>/* old burger */</script>'
)

# Body markers that must NEVER be touched by a chrome rewrite.
HERO = '<img src="/assets/heroes/draft-001.jpg" alt="A title" class="hero-img" />'
PUB_DATE = '<p class="art-meta">Published 2026-01-15 &middot; 4 min read</p>'
BODY = (
    '<section class="art-body"><div class="art-body-inner">'
    f'{HERO}\n{PUB_DATE}\n'
    '<p>Real article prose that must survive. Contains the word footer and header.</p>'
    '</div></section>'
)


def _article(nav: str, footer: str) -> str:
    """Assemble a published-article HTML the way publisher.build_article_html does."""
    return (
        "<!doctype html><html><head><style>.site-header { color: #000; }</style></head>\n"
        "<body>\n\n"
        f"{nav}\n\n"
        f"{BODY}\n\n"
        f"{footer}\n"
        "</body>\n</html>"
    )


class TestHeaderVariantConvergence:
    def test_text_wordmark_converges_to_canonical(self):
        html = _article(VARIANT_TEXT_WORDMARK, CANON_FOOTER)
        r = rebake_html(html, CANON_NAV, CANON_FOOTER)
        assert r.changed and not r.skipped
        assert CANON_NAV in r.new_html
        assert VARIANT_TEXT_WORDMARK not in r.new_html

    def test_light_logo_converges_to_canonical(self):
        html = _article(VARIANT_LIGHT_LOGO, CANON_FOOTER)
        r = rebake_html(html, CANON_NAV, CANON_FOOTER)
        assert r.changed and not r.skipped
        assert "logo-dark.svg" in r.new_html
        assert "logo-light.svg" not in r.new_html

    def test_already_current_header_is_unchanged(self):
        html = _article(CANON_NAV, CANON_FOOTER)
        r = rebake_html(html, CANON_NAV, CANON_FOOTER)
        assert not r.changed and not r.skipped


class TestFooterConvergence:
    def test_old_footer_converges_to_canonical(self):
        html = _article(CANON_NAV, OLD_FOOTER)
        r = rebake_html(html, CANON_NAV, CANON_FOOTER)
        assert r.changed and not r.skipped
        assert "&copy; 2026 Rolliq" in r.new_html
        assert "Old tagline." not in r.new_html
        assert "&copy; 2025 Rolliq" not in r.new_html

    def test_both_header_and_footer_rewritten_together(self):
        html = _article(VARIANT_TEXT_WORDMARK, OLD_FOOTER)
        r = rebake_html(html, CANON_NAV, CANON_FOOTER)
        assert r.changed
        assert CANON_NAV in r.new_html
        assert "AI automation, Wellington NZ." in r.new_html


class TestChromeOnly:
    def test_body_hero_and_publish_date_survive(self):
        html = _article(VARIANT_LIGHT_LOGO, OLD_FOOTER)
        r = rebake_html(html, CANON_NAV, CANON_FOOTER)
        assert HERO in r.new_html
        assert PUB_DATE in r.new_html
        assert "Real article prose that must survive" in r.new_html
        # Inline article CSS in <head> is left intact.
        assert ".site-header { color: #000; }" in r.new_html


class TestIdempotency:
    def test_rerun_on_rebaked_output_is_noop(self):
        html = _article(VARIANT_TEXT_WORDMARK, OLD_FOOTER)
        once = rebake_html(html, CANON_NAV, CANON_FOOTER)
        assert once.changed
        twice = rebake_html(once.new_html, CANON_NAV, CANON_FOOTER)
        assert not twice.changed and not twice.skipped
        assert twice.new_html == once.new_html

    def test_already_canonical_file_is_noop(self):
        html = _article(CANON_NAV, CANON_FOOTER)
        r = rebake_html(html, CANON_NAV, CANON_FOOTER)
        assert not r.changed and not r.skipped
        assert r.new_html == html


class TestSkipAndReport:
    def test_multiple_header_blocks_are_skipped(self):
        html = _article(VARIANT_TEXT_WORDMARK, CANON_FOOTER)
        # Inject a second header block — count != 1 must skip, not corrupt.
        html = html.replace("<body>\n", f"<body>\n{CANON_NAV}\n")
        r = rebake_html(html, CANON_NAV, CANON_FOOTER)
        assert r.skipped and not r.changed
        assert r.header_count == 2
        assert r.new_html == html
        assert "header block count = 2" in r.reason

    def test_zero_header_blocks_are_skipped(self):
        html = (
            "<!doctype html><html><body>\n"
            f"{BODY}\n{CANON_FOOTER}\n</body></html>"
        )
        r = rebake_html(html, CANON_NAV, CANON_FOOTER)
        assert r.skipped and not r.changed
        assert r.header_count == 0
        assert "header block count = 0" in r.reason

    def test_zero_footer_blocks_are_skipped(self):
        html = (
            "<!doctype html><html><body>\n"
            f"{CANON_NAV}\n{BODY}\n</body></html>"
        )
        r = rebake_html(html, CANON_NAV, CANON_FOOTER)
        assert r.skipped and not r.changed
        assert r.footer_count == 0
        assert "footer block count = 0" in r.reason


class TestRebakeDir:
    def _seed(self, tmp_path: Path) -> Path:
        articles = tmp_path / "articles"
        articles.mkdir()
        (articles / "stale.html").write_text(
            _article(VARIANT_TEXT_WORDMARK, OLD_FOOTER), encoding="utf-8"
        )
        (articles / "current.html").write_text(
            _article(CANON_NAV, CANON_FOOTER), encoding="utf-8"
        )
        return articles

    def test_dry_run_writes_nothing(self, tmp_path):
        articles = self._seed(tmp_path)
        before = (articles / "stale.html").read_text(encoding="utf-8")
        results = rebake_dir(articles, CANON_NAV, CANON_FOOTER, write=False)
        assert (articles / "stale.html").read_text(encoding="utf-8") == before
        assert all(not fr.written for fr in results)
        stale = next(fr for fr in results if fr.path.name == "stale.html")
        assert stale.result.changed

    def test_write_applies_and_is_idempotent(self, tmp_path):
        articles = self._seed(tmp_path)
        rebake_dir(articles, CANON_NAV, CANON_FOOTER, write=True)
        written = (articles / "stale.html").read_text(encoding="utf-8")
        assert CANON_NAV in written
        assert "logo-light.svg" not in written
        assert "Old tagline." not in written
        # Second pass over now-current files writes nothing.
        results = rebake_dir(articles, CANON_NAV, CANON_FOOTER, write=True)
        assert all(not fr.written for fr in results)
        assert all(not fr.result.changed for fr in results)
