"""
Tests for the two-stage Tue-draft → sign-off → Thu-publish gate.

The core safety property (issue: two-stage publish cadence): a draft enqueued by
the Tuesday stage is HELD and must NOT be publishable until a human approves it.
`queue_manager next` is the assertion point the Thursday publish workflow calls —
under the status model it withholds anything that isn't 'approved'.
"""

import argparse
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import queue_manager  # noqa: E402
from pipeline.brand_loader import load_brand  # noqa: E402

# A minimal but complete brand.yaml (all BrandConfig-required fields present),
# opted into the status-flag approval model — the model that provides the gate.
_BRAND_YAML = {
    "brand": "testbrand",
    "display_name": "Test Brand",
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


@pytest.fixture
def status_brand(tmp_path, monkeypatch):
    brand_dir = tmp_path / "brands" / "testbrand"
    (brand_dir / "staging" / "drafts").mkdir(parents=True)
    (brand_dir / "brand.yaml").write_text(yaml.safe_dump(_BRAND_YAML), encoding="utf-8")
    monkeypatch.setenv("MARKETING_REPO_ROOT", str(tmp_path))
    # start with an empty queue
    (brand_dir / "staging" / "publish_queue.json").write_text("[]\n", encoding="utf-8")
    return "testbrand"


def _ns(**kw):
    return argparse.Namespace(**kw)


def _next_env(brand, capsys):
    """Run `queue_manager next --output-env` and return the parsed env dict."""
    queue_manager.cmd_next(_ns(brand=brand, output_env=True))
    out = capsys.readouterr().out
    env = {}
    for line in out.splitlines():
        if "=" in line and line.split("=", 1)[0].isupper():
            k, v = line.split("=", 1)
            env[k] = v
    return env


class TestTwoStageGate:
    def test_add_enqueues_as_held_queued(self, status_brand, capsys):
        queue_manager.cmd_add(
            _ns(
                brand=status_brand,
                draft_path="staging/drafts/draft-011-v1.md",
                slug="my-slug",
                description="desc",
                gate_failed=False,
            )
        )
        queue = json.loads(
            (load_brand(status_brand).queue_path).read_text(encoding="utf-8")
        )
        assert len(queue) == 1
        assert queue[0]["status"] == "queued"
        assert queue[0]["slug"] == "my-slug"

    def test_unapproved_draft_is_not_publishable(self, status_brand, capsys):
        # Enqueue a held draft, then ask the publish workflow's selector for the
        # next thing to publish — it must return NOTHING.
        queue_manager.cmd_add(
            _ns(
                brand=status_brand,
                draft_path="staging/drafts/draft-011-v1.md",
                slug="my-slug",
                description="desc",
                gate_failed=False,
            )
        )
        env = _next_env(status_brand, capsys)
        assert env.get("QUEUE_DRAFT_PATH", "") == "", "a queued-but-unapproved draft must not be publishable"

    def test_approval_makes_it_publishable(self, status_brand, capsys):
        queue_manager.cmd_add(
            _ns(
                brand=status_brand,
                draft_path="staging/drafts/draft-011-v1.md",
                slug="my-slug",
                description="desc",
                gate_failed=False,
            )
        )
        queue_manager.cmd_approve(_ns(brand=status_brand, slug="my-slug", push=False))
        env = _next_env(status_brand, capsys)
        assert env.get("QUEUE_DRAFT_PATH") == "staging/drafts/draft-011-v1.md"
        assert env.get("QUEUE_SLUG") == "my-slug"

    def test_add_is_idempotent(self, status_brand, capsys):
        args = _ns(
            brand=status_brand,
            draft_path="staging/drafts/draft-011-v1.md",
            slug="my-slug",
            description="desc",
            gate_failed=False,
        )
        queue_manager.cmd_add(args)
        queue_manager.cmd_add(args)  # second call must not duplicate
        queue = json.loads(load_brand(status_brand).queue_path.read_text(encoding="utf-8"))
        assert len(queue) == 1

    def test_gate_failed_flag_recorded(self, status_brand, capsys):
        queue_manager.cmd_add(
            _ns(
                brand=status_brand,
                draft_path="staging/drafts/draft-012-v1.md",
                slug="bad-slug",
                description="d",
                gate_failed=True,
            )
        )
        queue = json.loads(load_brand(status_brand).queue_path.read_text(encoding="utf-8"))
        assert queue[0]["gate_passed"] is False
