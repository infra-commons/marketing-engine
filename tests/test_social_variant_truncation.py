"""
The newsletter excerpt and the X/Twitter post are cut on a word boundary
(pipeline/publisher.generate_social_variants).

Both files are read by a person — one by a subscriber, one by a follower — and both were
produced by a bare character slice (300 and 200) with an ellipsis appended on overflow,
the same defect that shipped a mid-word `<meta name="description">`. These assert the
boundary, not the length.
"""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.brand_loader import load_brand  # noqa: E402
from pipeline.publisher import generate_social_variants  # noqa: E402

FIXTURE_ROOT = Path(__file__).parent / "fixtures"

# A single paragraph well over both limits, so each cut has a boundary to find.
LONG_PARA = (
    "Employers reworking their payroll for the new minimum wage are discovering that the "
    "headline hourly rate is the smallest part of the bill, because leave loading and "
    "holiday pay both compound off it every single pay run, and the compounding is what "
    "turns a modest-looking rate change into a materially larger wage bill than the "
    "announcement suggested it would be for a business of any real size."
)


@pytest.fixture()
def brand_cfg(tmp_path, monkeypatch):
    # Copied into tmp_path rather than used in place: generate_social_variants WRITES into
    # brand_cfg.social_dir, and the fixture brand lives in the repository tree.
    dest = tmp_path / "brands" / "testbrand"
    dest.mkdir(parents=True)
    shutil.copy(FIXTURE_ROOT / "brands" / "testbrand" / "brand.yaml", dest / "brand.yaml")
    monkeypatch.setenv("MARKETING_REPO_ROOT", str(tmp_path))
    return load_brand("testbrand")


def _variants(brand_cfg, body):
    generate_social_variants(
        title="What The Wage Change Actually Costs",
        body=body,
        slug="what-the-wage-change-costs",
        brief=None,
        pub_date="2026-09-04",
        brand_cfg=brand_cfg,
    )
    def read(name):
        return (brand_cfg.social_dir / name).read_text(encoding="utf-8")

    return (
        read("newsletter-what-the-wage-change-costs.txt"),
        read("x-what-the-wage-change-costs.txt"),
    )


def _excerpt(text: str, limit: int) -> str:
    """The truncated paragraph inside a variant file, ellipsis stripped."""
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith(LONG_PARA[:40]):
            assert len(line) <= limit, f"{len(line)} > {limit}"
            return line.rstrip("…")
    raise AssertionError(f"no excerpt line found in:\n{text}")


class TestBoundaryCuts:
    def test_newsletter_excerpt_does_not_end_mid_word(self, brand_cfg):
        newsletter, _ = _variants(brand_cfg, LONG_PARA)
        kept = _excerpt(newsletter, 300)
        assert LONG_PARA.startswith(kept)
        assert LONG_PARA[len(kept)] == " "

    def test_x_post_does_not_end_mid_word(self, brand_cfg):
        _, x_post = _variants(brand_cfg, LONG_PARA)
        kept = _excerpt(x_post, 200)
        assert LONG_PARA.startswith(kept)
        assert LONG_PARA[len(kept)] == " "

    def test_both_mark_that_something_was_removed(self, brand_cfg):
        newsletter, x_post = _variants(brand_cfg, LONG_PARA)
        assert "…" in newsletter
        assert "…" in x_post

    def test_a_short_paragraph_is_untouched_and_unellipsised(self, brand_cfg):
        short = "The headline rate is the smallest part of the bill."
        newsletter, x_post = _variants(brand_cfg, short)
        assert short in newsletter
        assert short in x_post
        assert "…" not in newsletter
        assert "…" not in x_post

    def test_the_x_post_still_carries_the_article_url(self, brand_cfg):
        # The cut must not eat the link the post exists to carry.
        _, x_post = _variants(brand_cfg, LONG_PARA)
        assert f"{brand_cfg.article_url_base}/what-the-wage-change-costs.html" in x_post
