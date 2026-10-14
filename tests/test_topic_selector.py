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

    def test_a_leading_thinking_block_is_skipped(self, monkeypatch):
        """With adaptive thinking on, content[0] is a thinking block, not the reply."""

        class Thinking:
            type = "thinking"
            thinking = "reasoning"

        plain = _FakeResponse

        def with_thinking(text, stop_reason):
            response = plain(text, stop_reason)
            response.content.insert(0, Thinking())
            return response

        monkeypatch.setattr(sys.modules[__name__], "_FakeResponse", with_thinking)
        _fake_anthropic(monkeypatch, '{"ok": true}', "end_turn")
        assert topic_selector._call_claude("prompt", "key") == '{"ok": true}'


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


# ─────────────────────────────────────────────────────────────────────────────
# Brand editorial rules (workflow.article_types / editorial_rules / timely)
#
# A brand that bans a type must never receive it: cashbucket-com/marketing#332 got a
# news-reaction brief because the prompt offered every type, asked for a "timely" pick,
# and relabelled any unrecognised type as news-reaction. Brands that set none of the
# keys must behave exactly as before.
# ─────────────────────────────────────────────────────────────────────────────

EVERGREEN = {
    "article_types": ["how-to", "explainer"],
    "editorial_rules": ["SME-finance how-tos and explainers only; no news-reaction pieces"],
    "timely": False,
}


def _set_workflow(brand_slug, **keys):
    cfg = load_brand(brand_slug)
    data = yaml.safe_load((cfg.brand_dir / "brand.yaml").read_text(encoding="utf-8"))
    data["workflow"].update(keys)
    (cfg.brand_dir / "brand.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def evergreen_brand(brand):
    _set_workflow(brand, **EVERGREEN)
    return brand


def _scripted_claude(monkeypatch, *replies):
    """Patch _call_claude to return replies in order; returns the prompts it was sent."""
    prompts = []

    def fake(prompt, key, max_tokens=topic_selector.MAX_TOKENS):
        prompts.append(prompt)
        return replies[len(prompts) - 1]

    monkeypatch.setattr(topic_selector, "_call_claude", fake)
    return prompts


def _reply(article_type):
    return f'{{"topic_statement": "X", "article_type": "{article_type}", "angle": "a"}}'


CANDS = [{"title": "Opposition releases 47-point plan", "summary": "", "link": "https://n.test/p"}]


class TestEditorialPrompt:
    def test_unconfigured_brand_offers_every_type_and_a_timely_pick(self, brand):
        prompt = topic_selector._synthesis_prompt(load_brand(brand), CANDS, None)
        assert str(list(topic_selector.VALID_ARTICLE_TYPES)) in prompt
        assert "timely SME article" in prompt
        assert "timely angle" in prompt
        assert "EDITORIAL RULES" not in prompt

    def test_restricted_brand_offers_only_its_types_and_its_rules(self, evergreen_brand):
        prompt = topic_selector._synthesis_prompt(load_brand(evergreen_brand), CANDS, None)
        assert "one of ['how-to', 'explainer']" in prompt
        assert "'news-reaction'" not in prompt  # never offered as a type
        assert "EDITORIAL RULES" in prompt
        assert "no news-reaction pieces" in prompt

    def test_timely_false_drops_the_timely_framing(self, evergreen_brand):
        cfg = load_brand(evergreen_brand)
        for prompt in (
            topic_selector._synthesis_prompt(cfg, CANDS, None),
            topic_selector._synthesis_prompt(cfg, [], "operator topic"),
        ):
            assert "timely" not in prompt.lower()
            assert "evergreen" in prompt


class TestEditorialValidation:
    def test_allowed_pick_needs_one_call(self, evergreen_brand, monkeypatch):
        prompts = _scripted_claude(monkeypatch, _reply("how-to"))
        brief = topic_selector.synthesize_brief(load_brand(evergreen_brand), CANDS, None, api_key="k")
        assert brief["article_type"] == "how-to"
        assert len(prompts) == 1

    def test_banned_pick_is_repicked_with_the_reason(self, evergreen_brand, monkeypatch):
        prompts = _scripted_claude(monkeypatch, _reply("news-reaction"), _reply("explainer"))
        brief = topic_selector.synthesize_brief(load_brand(evergreen_brand), CANDS, None, api_key="k")
        assert brief["article_type"] == "explainer"
        assert len(prompts) == 2
        assert prompts[1].startswith(prompts[0])
        assert "REJECTED" in prompts[1] and '"news-reaction"' in prompts[1]

    def test_unrecognised_pick_is_repicked_not_relabelled(self, evergreen_brand, monkeypatch):
        # The re-pick answers "explainer", not the first allowed type, so a relabel
        # (which would yield "how-to") cannot pass this test.
        prompts = _scripted_claude(monkeypatch, _reply("bogus"), _reply("explainer"))
        brief = topic_selector.synthesize_brief(load_brand(evergreen_brand), [], "t", api_key="k")
        assert brief["article_type"] == "explainer"
        assert len(prompts) == 2

    def test_two_banned_picks_raise(self, evergreen_brand, monkeypatch):
        _scripted_claude(monkeypatch, _reply("news-reaction"), _reply("bogus"))
        with pytest.raises(topic_selector.EditorialRuleError):
            topic_selector.synthesize_brief(load_brand(evergreen_brand), CANDS, None, api_key="k")

    def test_unconfigured_brand_still_falls_back_to_news_reaction(self, brand, monkeypatch):
        prompts = _scripted_claude(monkeypatch, _reply("bogus"))
        brief = topic_selector.synthesize_brief(load_brand(brand), [], "t", api_key="k")
        assert brief["article_type"] == "news-reaction"
        assert len(prompts) == 1  # no re-pick for a brand that set no rules

    def test_select_and_write_fails_loudly_and_writes_nothing(
        self, evergreen_brand, monkeypatch, capsys
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        _scripted_claude(monkeypatch, _reply("news-reaction"), _reply("news-reaction"))
        with pytest.raises(SystemExit) as exc_info:
            topic_selector.select_and_write(evergreen_brand, override_topic="t", verbose=False)
        assert exc_info.value.code == 4
        assert "editorial rules" in capsys.readouterr().err
        assert list(load_brand(evergreen_brand).briefs_dir.glob("brief-*.json")) == []

    def test_bad_config_fails_before_any_model_call(self, brand, monkeypatch, capsys):
        _set_workflow(brand, article_types=["how-to", "listicle"])
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        prompts = _scripted_claude(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            topic_selector.select_and_write(brand, override_topic="t", verbose=False)
        assert exc_info.value.code == 4
        assert "listicle" in capsys.readouterr().err
        assert prompts == []
