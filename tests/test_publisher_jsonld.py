"""
Unit tests for the Article JSON-LD block emitted by build_article_html.

Asserts every generated article page carries a valid `@type: Article`
schema.org object in a <script type="application/ld+json"> tag. Runs in CI
without external dependencies (reads brand config from the consuming repo,
read-only).
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.brand_loader import load_brand  # noqa: E402
from pipeline.publisher import build_article_html  # noqa: E402

CONSUMER_ROOT = Path(__file__).parent.parent.parent


@pytest.fixture()
def brand_cfg(monkeypatch):
    monkeypatch.setenv("MARKETING_REPO_ROOT", str(CONSUMER_ROOT))
    return load_brand("rolliq")


def _render(brand_cfg, **overrides):
    kwargs = dict(
        title="How NZ SMBs Adopt Automation",
        slug="how-nz-smbs-adopt-automation",
        body_html="<p>Body.</p>",
        citations_html="",
        article_type="guide",
        description="A guide for NZ SMBs.",
        pub_date="2026-07-13",
        mins=4,
        brand_cfg=brand_cfg,
        nav_html="<nav></nav>",
        footer_html="<footer></footer>",
    )
    kwargs.update(overrides)
    return build_article_html(**kwargs)


def _extract_jsonld(html: str) -> dict:
    m = re.search(
        r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>',
        html,
        re.DOTALL,
    )
    assert m, "no application/ld+json script block in generated article HTML"
    return json.loads(m.group(1).replace("<\\/", "</"))


class TestArticleJsonLd:
    def test_emits_article_schema(self, brand_cfg):
        obj = _extract_jsonld(_render(brand_cfg))
        assert obj["@context"] == "https://schema.org"
        assert obj["@type"] == "Article"
        assert obj["headline"] == "How NZ SMBs Adopt Automation"
        assert obj["datePublished"] == "2026-07-13"
        assert obj["url"] == f"{brand_cfg.article_url_base}/how-nz-smbs-adopt-automation.html"
        assert obj["mainEntityOfPage"]["@id"] == obj["url"]
        assert obj["author"]["@type"] == "Organization"
        assert obj["publisher"]["name"] == brand_cfg.display_name
        assert obj["inLanguage"] == "en-NZ"
        assert "image" not in obj

    def test_hero_image_becomes_absolute_url(self, brand_cfg):
        obj = _extract_jsonld(_render(brand_cfg, hero_img_path="/assets/hero.png"))
        assert obj["image"] == f"{brand_cfg.site_url}/assets/hero.png"

    def test_script_close_tag_in_title_is_escaped(self, brand_cfg):
        html = _render(brand_cfg, title="Bad </script> Title")
        obj = _extract_jsonld(html)
        # The raw block must not contain an unescaped terminator inside a value.
        assert obj["headline"] == "Bad </script> Title"
