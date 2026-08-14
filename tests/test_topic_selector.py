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


# ─────────────────────────────────────────────────────────────────────────────
# Truncated-response handling (infra-commons/marketing-engine#22)
#
# "Unterminated string" is the signature of a response cut off by the max_tokens
# cap, not of malformed generation. These cover: detecting that explicitly via
# stop_reason (not by inferring it from the parse error), retrying once with a
# higher cap, and failing loudly — never silently — when that's exhausted.
# ─────────────────────────────────────────────────────────────────────────────


class _FakeContentBlock:
    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text, stop_reason):
        self.content = [_FakeContentBlock(text)]
        self.stop_reason = stop_reason


def _fake_anthropic(monkeypatch, text, stop_reason, captured=None):
    """Patch anthropic.Anthropic with a client whose messages.create() returns a
    canned (text, stop_reason) response. captured, if given, collects each call's
    kwargs so tests can assert on the max_tokens actually sent."""

    class FakeMessages:
        def create(self, **kwargs):
            if captured is not None:
                captured.append(kwargs)
            return _FakeResponse(text, stop_reason)

    class FakeClient:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    monkeypatch.setattr("anthropic.Anthropic", FakeClient)


class TestCallClaudeTruncation:
    def test_max_tokens_stop_reason_raises_truncated_error(self, monkeypatch):
        _fake_anthropic(monkeypatch, '{"topic_statement": "cut off mid', "max_tokens")
        with pytest.raises(topic_selector.TruncatedResponseError):
            topic_selector._call_claude("prompt", "key")

    def test_end_turn_stop_reason_returns_text(self, monkeypatch):
        _fake_anthropic(monkeypatch, '{"ok": true}', "end_turn")
        assert topic_selector._call_claude("prompt", "key") == '{"ok": true}'

    def test_retry_max_tokens_is_passed_through(self, monkeypatch):
        captured = []
        _fake_anthropic(monkeypatch, '{"ok": true}', "end_turn", captured=captured)
        topic_selector._call_claude("prompt", "key", max_tokens=topic_selector.RETRY_MAX_TOKENS)
        assert captured[0]["max_tokens"] == topic_selector.RETRY_MAX_TOKENS


class TestSynthesizeBriefTruncationRetry:
    def test_retries_once_after_truncation_then_succeeds(self, brand, monkeypatch):
        # Regression test for #22 — fails against pre-fix code (no retry, no
        # TruncatedResponseError).
        cfg = load_brand(brand)
        calls = []

        def fake_call_claude(prompt, key, max_tokens=topic_selector.MAX_TOKENS):
            calls.append(max_tokens)
            if len(calls) == 1:
                raise topic_selector.TruncatedResponseError(
                    "model response was truncated at the 2000-token cap (stop_reason=max_tokens)"
                )
            return '{"topic_statement": "X", "article_type": "explainer", "angle": "a"}'

        monkeypatch.setattr(topic_selector, "_call_claude", fake_call_claude)
        brief = topic_selector.synthesize_brief(cfg, [], "some topic", api_key="k")
        assert brief["topic_statement"] == "X"
        assert calls == [topic_selector.MAX_TOKENS, topic_selector.RETRY_MAX_TOKENS]

    def test_truncated_on_retry_too_propagates_not_swallowed(self, brand, monkeypatch):
        cfg = load_brand(brand)

        def fake_call_claude(prompt, key, max_tokens=topic_selector.MAX_TOKENS):
            raise topic_selector.TruncatedResponseError(
                "model response was truncated (stop_reason=max_tokens)"
            )

        monkeypatch.setattr(topic_selector, "_call_claude", fake_call_claude)
        with pytest.raises(topic_selector.TruncatedResponseError):
            topic_selector.synthesize_brief(cfg, [], "some topic", api_key="k")


class TestSelectAndWriteTruncationIsLoud:
    def test_exhausted_retry_fails_loudly_not_silently(self, brand, monkeypatch, capsys):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

        def fake_call_claude(prompt, key, max_tokens=topic_selector.MAX_TOKENS):
            raise topic_selector.TruncatedResponseError(
                "model response was truncated (stop_reason=max_tokens)"
            )

        monkeypatch.setattr(topic_selector, "_call_claude", fake_call_claude)
        with pytest.raises(SystemExit) as exc_info:
            topic_selector.select_and_write(brand, override_topic="some topic", verbose=False)
        assert exc_info.value.code == 3
        assert "truncat" in capsys.readouterr().err.lower()
