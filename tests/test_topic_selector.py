"""
Tests for pipeline/topic_selector.py — feed parsing, dedupe, candidate collection.

Network (feed fetch) and the Claude call are both mocked, so these run offline in CI.
"""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import topic_selector  # noqa: E402
from pipeline.brand_loader import load_brand  # noqa: E402

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Feed</title>
  <item><title>RBNZ holds OCR at 3.50% in surprise call</title>
        <link>https://news.example/ocr</link>
        <description>The Reserve Bank &lt;b&gt;held&lt;/b&gt; rates.</description></item>
  <item><title>NZ minimum wage rises to $23.50 an hour</title>
        <link>https://news.example/minwage</link>
        <description>From April.</description></item>
</channel></rss>"""

ATOM_SAMPLE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>IRD tightens provisional tax rules</title>
         <link href="https://news.example/ird"/>
         <summary>Changes ahead.</summary></entry>
</feed>"""


class TestParseFeed:
    def test_parses_rss_items(self):
        items = topic_selector.parse_feed(RSS_SAMPLE)
        assert len(items) == 2
        assert items[0]["title"] == "RBNZ holds OCR at 3.50% in surprise call"
        assert items[0]["link"] == "https://news.example/ocr"
        assert "held rates" in items[0]["summary"]  # HTML stripped

    def test_parses_atom_entries_with_href_link(self):
        items = topic_selector.parse_feed(ATOM_SAMPLE)
        assert len(items) == 1
        assert items[0]["title"] == "IRD tightens provisional tax rules"
        assert items[0]["link"] == "https://news.example/ird"

    def test_bad_xml_returns_empty(self):
        assert topic_selector.parse_feed("<not xml") == []


class TestOverlap:
    def test_detects_covered_topic(self):
        cand = topic_selector._keywords("RBNZ holds OCR at 3.50%")
        covered = [topic_selector._keywords("What the RBNZ OCR hold means for cashflow")]
        assert topic_selector._overlaps(cand, covered) is True

    def test_distinct_topic_not_flagged(self):
        cand = topic_selector._keywords("NZ minimum wage rises to $23.50")
        covered = [topic_selector._keywords("RBNZ holds OCR at 3.50%")]
        assert topic_selector._overlaps(cand, covered) is False


_BRAND_YAML = {
    "brand": "testbrand", "display_name": "Test Brand", "site_repo": "t/s",
    "site_local_name": "site", "site_url": "https://e.test", "gh_token_env": "GH",
    "unsplash_key_env": "U", "articles_path": "a", "articles_index": "a.html",
    "assets_path": "as", "articles_grid_marker": "<!--g-->",
    "article_url_base": "https://e.test/a", "unsplash_utm_source": "t",
    "cta_booking_url": "https://e.test/b", "cta_contact_url": "https://e.test/c",
    "cta_headline": "h", "cta_body": "b", "cta_btn_primary": "p",
    "cta_btn_secondary": "s", "tagline": "t", "platform_description": "NZ SMEs",
    "brand_section_label": "Test", "colors": {"primary": "#000"},
    "hero_gradients": {"default": "x"}, "article_type_labels": {}, "social": {},
    "workflow": {"approval": "status", "draft_dir": "drafts"},
}


@pytest.fixture
def brand(tmp_path, monkeypatch):
    bd = tmp_path / "brands" / "testbrand"
    (bd / "staging" / "briefs").mkdir(parents=True)
    (bd / "brand.yaml").write_text(yaml.safe_dump(_BRAND_YAML), encoding="utf-8")
    (bd / "staging" / "publish_queue.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setenv("MARKETING_REPO_ROOT", str(tmp_path))
    return "testbrand"


class TestCollectCandidates:
    def test_dedupes_against_existing_brief(self, brand, monkeypatch):
        cfg = load_brand(brand)
        # Seed an existing brief covering the OCR story.
        (cfg.briefs_dir / "brief-001.json").write_text(
            '{"brief_id":"brief-001","topic_statement":"RBNZ holds the OCR at 3.50% and SME cashflow"}',
            encoding="utf-8",
        )
        monkeypatch.setattr(topic_selector, "_fetch_feed", lambda url, timeout=15: RSS_SAMPLE)
        cands = topic_selector.collect_candidates(cfg, ["http://feed"])
        titles = [c["title"] for c in cands]
        assert not any("OCR" in t for t in titles), "OCR story should be deduped against the existing brief"
        assert any("minimum wage" in t for t in titles)

    def test_dead_feed_is_skipped_not_fatal(self, brand, monkeypatch):
        cfg = load_brand(brand)

        def boom(url, timeout=15):
            raise OSError("connection refused")

        monkeypatch.setattr(topic_selector, "_fetch_feed", boom)
        assert topic_selector.collect_candidates(cfg, ["http://dead"]) == []

    def test_next_brief_id_increments(self, brand):
        cfg = load_brand(brand)
        (cfg.briefs_dir / "brief-007.json").write_text("{}", encoding="utf-8")
        assert topic_selector._next_brief_id(cfg) == "brief-008"


class TestSynthesizeBrief:
    def test_normalises_model_output(self, brand, monkeypatch):
        cfg = load_brand(brand)
        fake = (
            '```json\n{"brief_id":"PENDING","topic_statement":"X","article_type":"bogus",'
            '"angle":"a strong angle","dates_verified":true}\n```'
        )
        monkeypatch.setattr(topic_selector, "_call_claude", lambda prompt, key: fake)
        brief = topic_selector.synthesize_brief(cfg, [], "some topic", api_key="k")
        assert brief["brief_id"] == "brief-001"           # id assigned by caller
        assert brief["article_type"] == "news-reaction"    # invalid type normalised
        assert brief["dates_verified"] is False            # forced false for auto-gen
        assert brief["slug"]                               # slug derived
        assert brief["description"]
