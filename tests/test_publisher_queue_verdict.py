"""
`publisher` must refuse a blocked entry BEFORE it spends anything.

The refusal is only worth having where it is placed. This run's first side effect is not
writing the article page — it is step [4], which fetches a hero image, saves the asset into
the site checkout and records the photo ID in the brand workspace so that image is never
reused. A check that ran after that point would refuse correctly and still have consumed a
one-time resource on an article that never ships, and would have written into the brand
repo on a run that produced nothing.

So these tests assert the exit code AND that nothing was written. The second half is the
one that rots: moving the check three steps later leaves every exit-code assertion green.

`--dry-run` deliberately does not relax any of this. A dry run still writes the hero asset,
the article page, the index card, the social variants and the sitemap; only the push is
skipped. A rehearsal that passes where the real run refuses tells the operator the opposite
of what they asked it.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import publisher  # noqa: E402
from tests.test_two_stage_gate import _BRAND_YAML  # noqa: E402

# The shared fixture, which is built to PASS the compliance gate. That matters: step [2b]
# runs before the check under test, so an article that fails the gate would never reach
# [2c] and every assertion here would be passing for the wrong reason.
ARTICLE = (Path(__file__).parent / "fixtures" / "sample_article.md").read_text(encoding="utf-8")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A brand workspace with a draft on disk and a site checkout beside it."""
    brand_dir = tmp_path / "brands" / "testbrand"
    (brand_dir / "staging" / "drafts").mkdir(parents=True)
    (brand_dir / "staging" / "briefs").mkdir(parents=True)
    (brand_dir / "brand.yaml").write_text(yaml.safe_dump(_BRAND_YAML), encoding="utf-8")
    (brand_dir / "staging" / "drafts" / "draft-001-v1.md").write_text(ARTICLE, encoding="utf-8")

    site = tmp_path.parent / "site"
    (site / "articles").mkdir(parents=True, exist_ok=True)
    (site / "articles.html").write_text("<!-- grid -->\n", encoding="utf-8")

    monkeypatch.setenv("MARKETING_REPO_ROOT", str(tmp_path))
    # No Unsplash key: step [4] is a no-op for the image itself, but reaching it at all is
    # what these tests are about, so the assertion below is on the used-IDs ledger.
    monkeypatch.delenv("UNSPLASH_KEY", raising=False)
    return tmp_path, brand_dir, site


def write_queue(brand_dir, entry):
    (brand_dir / "staging" / "publish_queue.json").write_text(
        json.dumps([entry], indent=2), encoding="utf-8"
    )


def run(site, *extra):
    """Run the publisher; return its exit code, or None if it raised past the check.

    The unit under test is step [2c]. The shared brand fixture is deliberately minimal and
    does not carry enough config to render an article page, so a run that PASSES [2c] goes
    on to fail somewhere in [6/7] for reasons that have nothing to do with this file.
    `reached_slug_derivation` is what the pass-through cases assert on; the refusal cases
    assert on the exit code, which they reach long before any of that matters.
    """
    argv = [
        "publisher",
        "staging/drafts/draft-001-v1.md",
        "--brand", "testbrand",
        "--site-path", str(site),
        "--dry-run",
        *extra,
    ]
    saved, sys.argv = sys.argv, argv
    try:
        return publisher.main()
    except Exception:
        return None
    finally:
        sys.argv = saved


def reached_slug_derivation(captured) -> bool:
    """True if execution got past [2c] to the next step — i.e. the verdict check allowed it."""
    return "[3/7]" in captured.out


def wrote_anything(brand_dir, site) -> bool:
    """Any artefact this run could have left behind, in either repo."""
    return (
        (brand_dir / "unsplash-used.json").exists()
        or any((site / "articles").glob("*.html"))
        or (brand_dir / "staging" / "social").exists()
    )


def entry(**overrides):
    base = {
        "draft_path": "staging/drafts/draft-001-v1.md",
        "slug": "a-clear-and-ordinary-headline",
        "status": "approved",
        "gate_passed": True,
        "dates_verified": True,
    }
    base.update(overrides)
    return base


def test_an_unverified_entry_is_refused_and_writes_nothing(workspace, capsys):
    _root, brand_dir, site = workspace
    write_queue(brand_dir, entry(dates_verified=False))
    assert run(site) == 1
    assert "never checked against a primary source" in capsys.readouterr().err
    assert not wrote_anything(brand_dir, site), (
        "the refusal must land before step [4], which records a photo ID that can never be reused"
    )


def test_a_failed_gate_verdict_is_refused_and_writes_nothing(workspace):
    _root, brand_dir, site = workspace
    write_queue(brand_dir, entry(gate_passed=False, gate_flags=["a recorded flag"]))
    assert run(site) == 1
    assert not wrote_anything(brand_dir, site)


def test_a_stale_verdict_is_refused(workspace):
    _root, brand_dir, site = workspace
    write_queue(brand_dir, entry(gate_stale=True))
    assert run(site) == 1


def test_dry_run_does_not_relax_the_refusal(workspace):
    """`run` already passes --dry-run; this names the property that test relies on."""
    _root, brand_dir, site = workspace
    write_queue(brand_dir, entry(dates_verified=False))
    assert run(site) == 1


def test_force_gate_requires_a_reason(workspace, capsys):
    _root, brand_dir, site = workspace
    write_queue(brand_dir, entry(dates_verified=False))
    assert run(site, "--force-gate") == 1
    assert "--force-gate requires --reason" in capsys.readouterr().err


def test_force_gate_with_a_reason_proceeds(workspace, capsys):
    _root, brand_dir, site = workspace
    write_queue(brand_dir, entry(dates_verified=False))
    run(site, "--force-gate", "--reason", "figures confirmed against the source")
    captured = capsys.readouterr()
    assert reached_slug_derivation(captured)
    assert "forced past" in captured.out
    assert "figures confirmed against the source" in captured.out, "an override must record why"


def test_an_honoured_override_proceeds(workspace, capsys):
    _root, brand_dir, site = workspace
    write_queue(brand_dir, entry(dates_verified=False, gate_override={
        "by": "approver@example.test", "reason": "confirmed by hand", "at": "t", "blockers": ["dates"],
    }))
    run(site)
    captured = capsys.readouterr()
    assert reached_slug_derivation(captured)
    assert "gate_override honoured" in captured.out


def test_a_malformed_override_is_refused_rather_than_ignored(workspace):
    """Ignoring it would turn a bypass someone attempted into a refusal they never saw
    explained; honouring it would accept a bypass nobody can account for."""
    _root, brand_dir, site = workspace
    write_queue(brand_dir, entry(dates_verified=False, gate_override={"by": "someone"}))
    assert run(site) == 1


def test_a_clean_entry_is_allowed_through(workspace, capsys):
    """Without this the whole file would be satisfied by a check that refuses everything."""
    _root, brand_dir, site = workspace
    write_queue(brand_dir, entry())
    run(site)
    captured = capsys.readouterr()
    assert "✓ Queue records no blockers" in captured.out
    assert reached_slug_derivation(captured)


def test_a_draft_absent_from_the_queue_warns_and_proceeds(workspace, capsys):
    """"Do not publish what the record says is blocked" is this module's rule. "Do not
    publish what is not in the record" belongs to the caller, which knows its own workflow
    — a brand may not keep a queue at all."""
    _root, brand_dir, site = workspace
    write_queue(brand_dir, entry(draft_path="staging/drafts/some-other-draft.md"))
    run(site)
    captured = capsys.readouterr()
    assert reached_slug_derivation(captured)
    assert "not in publish_queue.json" in captured.out


def test_no_queue_file_at_all_is_not_an_error(workspace, capsys):
    _root, _brand_dir, site = workspace
    run(site)
    assert reached_slug_derivation(capsys.readouterr())
