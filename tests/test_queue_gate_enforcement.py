"""
The recorded verdict on a queue entry must bind the paths that can publish it.

The queue stores what is known about each article — whether the compliance gate passed,
whether the brief's figures were ever checked against a primary source — and for a long
time nothing between the queue and a published file read any of it. `approve` checked only
the status. `next` selected on status alone. `publisher` re-ran the compliance gate, which
scores PROSE and not truth, and never looked at the record at all. So the one property the
gate structurally cannot supply — is this figure real — was written down, and then ignored
by every consumer of the thing it was written on.

These tests pin the enforcement and, as importantly, its limits:

  * absence is not failure. An entry written before a flag existed carries no key, and a
    guard that blocks the whole backlog retroactively teaches an operator to click past
    it — which disarms it for the case where it is right;
  * `next` still EMITS a blocked entry, warning rather than withholding. Withholding would
    produce a green run indistinguishable from an empty queue, every cycle, for an article
    sitting blocked at the head of the queue;
  * `requeue` cannot launder an entry past the gate. It clears the verdict, so it must set
    `gate_stale` or the cleared entry would read as "unknown" and sail through;
  * `publisher` refuses BEFORE its first side effect, which is fetching a hero image — not
    before writing the article page, which happens several steps later.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import queue_manager  # noqa: E402
from pipeline.queue_policy import honoured_override, recorded_blockers  # noqa: E402
from tests.test_two_stage_gate import _BRAND_YAML, _ns  # noqa: E402


@pytest.fixture
def brand(tmp_path, monkeypatch):
    brand_dir = tmp_path / "brands" / "testbrand"
    (brand_dir / "staging" / "drafts").mkdir(parents=True)
    (brand_dir / "staging" / "briefs").mkdir(parents=True)
    (brand_dir / "brand.yaml").write_text(yaml.safe_dump(_BRAND_YAML), encoding="utf-8")
    (brand_dir / "staging" / "publish_queue.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setenv("MARKETING_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("QUEUE_APPROVER", "approver@example.test")
    return "testbrand"


def seed(brand_slug, entries):
    queue_manager.save_queue(brand_slug, entries)


def read(brand_slug):
    return queue_manager.load_queue(brand_slug)


def item(**overrides):
    base = {
        "draft_path": "staging/drafts/draft-001-v1.md",
        "slug": "a-slug",
        "description": "d",
        "status": "queued",
        "gate_passed": True,
        "dates_verified": True,
    }
    base.update(overrides)
    return base


# ── the shared predicate ──────────────────────────────────────────────────────────


def test_absence_is_not_failure():
    """The rule every reader depends on. An entry predating a flag is unknown, not failed."""
    assert recorded_blockers({"slug": "legacy"}) == []


def test_falsy_is_not_false():
    """`is False`, never truthiness: None and 0 are not a recorded failure."""
    assert recorded_blockers({"gate_passed": None, "dates_verified": 0}) == []


def test_each_blocker_is_reported_separately():
    reasons = recorded_blockers(
        {"gate_passed": False, "gate_flags": ["em dash"], "dates_verified": False, "gate_stale": True}
    )
    assert len(reasons) == 3, "they fail differently and are fixed differently"
    assert "em dash" in reasons[0]


def test_an_override_without_a_reason_is_rejected():
    """A bare boolean records that someone bypassed a check and nothing about why."""
    with pytest.raises(ValueError, match="no written reason"):
        honoured_override({"slug": "s", "gate_override": {"by": "someone", "at": "t"}})


# ── add ───────────────────────────────────────────────────────────────────────────


def test_add_records_the_verdict_both_ways(brand):
    queue_manager.cmd_add(_ns(brand=brand, draft_path="staging/drafts/draft-001-v1.md",
                              slug="clean", description="", gate_failed=False))
    queue_manager.cmd_add(_ns(brand=brand, draft_path="staging/drafts/draft-002-v1.md",
                              slug="dirty", description="", gate_failed=True))
    entries = read(brand)
    assert entries[0]["gate_passed"] is True, (
        "recording only failures made absence mean 'passed' OR 'never gated', and every "
        "reader then had to treat an ungated entry as a clean one"
    )
    assert entries[1]["gate_passed"] is False


def test_add_omits_dates_verified_when_the_brief_is_missing(brand, capsys):
    """Writing True on a miss would assert that figures were verified when nothing looked."""
    queue_manager.cmd_add(_ns(brand=brand, draft_path="staging/drafts/draft-001-v1.md",
                              slug="s", description="", gate_failed=False))
    assert "dates_verified" not in read(brand)[0]
    assert "no brief found" in capsys.readouterr().err


def test_add_copies_dates_verified_from_the_brief(brand):
    root = Path(queue_manager.load_brand(brand).briefs_dir)
    (root / "brief-001.json").write_text(json.dumps({"dates_verified": False}), encoding="utf-8")
    queue_manager.cmd_add(_ns(brand=brand, draft_path="staging/drafts/draft-001-v1.md",
                              slug="s", description="", gate_failed=False))
    assert read(brand)[0]["dates_verified"] is False


# ── approve ───────────────────────────────────────────────────────────────────────


def test_approve_refuses_a_failed_gate(brand, capsys):
    seed(brand, [item(gate_passed=False, gate_flags=["em dash in headline"])])
    assert queue_manager.cmd_approve(_ns(brand=brand, slug="a-slug", push=False, force=False, reason="")) == 1
    assert "em dash in headline" in capsys.readouterr().err
    assert read(brand)[0]["status"] == "queued", "a refused approve must write nothing"


def test_approve_refuses_unverified_figures(brand, capsys):
    """The blocker with no other line of defence: the gate cannot check a number."""
    seed(brand, [item(dates_verified=False)])
    assert queue_manager.cmd_approve(_ns(brand=brand, slug="a-slug", push=False, force=False, reason="")) == 1
    assert "never checked against a primary source" in capsys.readouterr().err


def test_approve_refuses_a_stale_verdict(brand):
    seed(brand, [item(gate_stale=True)])
    assert queue_manager.cmd_approve(_ns(brand=brand, slug="a-slug", push=False, force=False, reason="")) == 1


def test_force_without_a_reason_is_refused(brand, capsys):
    seed(brand, [item(dates_verified=False)])
    assert queue_manager.cmd_approve(_ns(brand=brand, slug="a-slug", push=False, force=True, reason="  ")) == 1
    assert "--force requires --reason" in capsys.readouterr().err
    assert read(brand)[0]["status"] == "queued"


def test_force_with_a_reason_approves_and_stamps_the_override(brand):
    seed(brand, [item(dates_verified=False)])
    assert queue_manager.cmd_approve(
        _ns(brand=brand, slug="a-slug", push=False, force=True, reason="confirmed against the source release")
    ) == 0
    entry = read(brand)[0]
    assert entry["status"] == "approved"
    stamp = entry["gate_override"]
    assert stamp["by"] == "approver@example.test"
    assert stamp["reason"] == "confirmed against the source release"
    assert stamp["at"].endswith("Z"), "the timestamp shape must match the other writer of this field"
    assert stamp["blockers"], "an override must record WHAT it overrode"


def test_a_clean_entry_still_approves(brand):
    """Without this the suite would be satisfied by refusing everything."""
    seed(brand, [item()])
    assert queue_manager.cmd_approve(_ns(brand=brand, slug="a-slug", push=False, force=False, reason="")) == 0
    entry = read(brand)[0]
    assert entry["status"] == "approved"
    assert entry["approved_by"] == "approver@example.test"
    assert entry["approved_at"].endswith("Z")


def test_a_legacy_entry_with_no_verdict_still_approves(brand):
    """Absence is unknown, not failed — see the module docstring."""
    seed(brand, [{"draft_path": "staging/drafts/draft-001-v1.md", "slug": "a-slug", "status": "queued"}])
    assert queue_manager.cmd_approve(_ns(brand=brand, slug="a-slug", push=False, force=False, reason="")) == 0


def test_approve_points_a_stranded_entry_at_requeue(brand, capsys):
    seed(brand, [item(status="pending_review")])
    assert queue_manager.cmd_approve(_ns(brand=brand, slug="a-slug", push=False, force=False, reason="")) == 1
    assert "requeue a-slug" in capsys.readouterr().err


# ── next ──────────────────────────────────────────────────────────────────────────


def test_next_warns_but_still_emits(brand, capsys):
    """The anti-regression for the shape where a blocked queue looks like an empty one.

    Withholding here would emit empty keys and exit 0 — a green run, repeating every
    cycle, for an article sitting blocked at the head of the queue, and indistinguishable
    from the ordinary nothing-to-do case. The refusal belongs downstream, where it can
    name the article and the reason.
    """
    seed(brand, [item(status="approved", dates_verified=False)])
    assert queue_manager.cmd_next(_ns(brand=brand, output_env=True)) == 0
    captured = capsys.readouterr()
    assert "QUEUE_DRAFT_PATH=staging/drafts/draft-001-v1.md" in captured.out
    assert "QUEUE_DRAFT_PATH=\n" not in captured.out
    assert "QUEUE VERDICT" in captured.err


def test_an_empty_queue_is_still_a_clean_no_op(brand, capsys):
    seed(brand, [])
    assert queue_manager.cmd_next(_ns(brand=brand, output_env=True)) == 0
    assert "QUEUE_DRAFT_PATH=" in capsys.readouterr().out


# ── dates verification ────────────────────────────────────────────────────────────


def test_dates_check_fires_for_every_article_type(brand):
    """It was scoped to one article type and so had never fired on anything produced."""
    cfg = queue_manager.load_brand(brand)
    (cfg.briefs_dir / "brief-001.json").write_text(
        json.dumps({"article_type": "sector-analysis", "dates_verified": False}), encoding="utf-8"
    )
    assert queue_manager._check_dates_verified(item(), cfg) is not None


def test_dates_check_is_quiet_when_the_brief_is_verified(brand):
    cfg = queue_manager.load_brand(brand)
    (cfg.briefs_dir / "brief-001.json").write_text(
        json.dumps({"article_type": "explainer", "dates_verified": True}), encoding="utf-8"
    )
    assert queue_manager._check_dates_verified(item(), cfg) is None


def test_an_unreadable_brief_does_not_silently_disable_the_check(brand):
    """A guard that answers 'no problem' when it could not look is the failure mode here."""
    cfg = queue_manager.load_brand(brand)
    assert queue_manager._check_dates_verified(item(dates_verified=False), cfg) is not None
    assert queue_manager._check_dates_verified(item(dates_verified=True), cfg) is None


# ── requeue ───────────────────────────────────────────────────────────────────────


def test_requeue_gives_pending_review_an_exit(brand):
    seed(brand, [item(status="pending_review")])
    assert queue_manager.cmd_requeue(_ns(brand=brand, slug="a-slug", reason="revisiting")) == 0
    entry = read(brand)[0]
    assert entry["status"] == "queued"
    assert entry["requeued_reason"] == "revisiting"
    assert entry["requeued_at"]


def test_requeue_also_releases_hold(brand):
    seed(brand, [item(status="hold")])
    assert queue_manager.cmd_requeue(_ns(brand=brand, slug="a-slug", reason="")) == 0
    assert read(brand)[0]["status"] == "queued"


def test_requeue_clears_every_stale_verdict_field(brand):
    seed(brand, [item(
        status="pending_review", gate_passed=True, gate_flags=[], gate_warnings=["w"],
        dates_verified=True, word_count=900,
        gate_override={"by": "x", "reason": "r", "at": "t", "blockers": []},
        approved_by="x", approved_at="t",
    )])
    queue_manager.cmd_requeue(_ns(brand=brand, slug="a-slug", reason=""))
    entry = read(brand)[0]
    for key in queue_manager._VERDICT_KEYS:
        assert key not in entry, f"{key} describes a version of the draft that no longer applies"


def test_requeue_cannot_launder_an_entry_past_the_gate(brand):
    """The safety property. Clearing the verdict makes the entry read as 'unknown', and
    unknown does not block — so requeue must mark it stale or it becomes a route straight
    to `approved` with the gate never having run."""
    seed(brand, [item(status="pending_review", gate_passed=True, dates_verified=True)])
    queue_manager.cmd_requeue(_ns(brand=brand, slug="a-slug", reason=""))
    entry = read(brand)[0]
    assert entry["gate_stale"] is True
    assert recorded_blockers(entry), "a requeued entry must still block until it is re-gated"
    assert queue_manager.cmd_approve(_ns(brand=brand, slug="a-slug", push=False, force=False, reason="")) == 1


def test_requeue_is_idempotent_on_queued(brand):
    seed(brand, [item(status="queued")])
    assert queue_manager.cmd_requeue(_ns(brand=brand, slug="a-slug", reason="")) == 0
    assert "gate_stale" not in read(brand)[0], "a no-op must not invalidate a live verdict"


def test_requeue_refuses_published_and_approved(brand):
    seed(brand, [item(status="published")])
    assert queue_manager.cmd_requeue(_ns(brand=brand, slug="a-slug", reason="")) == 1
    seed(brand, [item(status="approved")])
    assert queue_manager.cmd_requeue(_ns(brand=brand, slug="a-slug", reason="")) == 1


def test_requeue_of_an_unknown_slug_is_loud(brand):
    seed(brand, [item()])
    assert queue_manager.cmd_requeue(_ns(brand=brand, slug="nope", reason="")) == 1


# ── status ────────────────────────────────────────────────────────────────────────


def test_status_shows_pending_review_under_the_status_model(brand, capsys):
    """It was printed only under the other approval model, so entries stranded here were
    counted by the one function that could have reported them and never displayed."""
    seed(brand, [item(status="pending_review")])
    queue_manager.cmd_status(_ns(brand=brand))
    out = capsys.readouterr().out
    assert "Pending review:" in out
    assert "requeue <slug>" in out


def test_status_shows_the_gate_column_and_names_blockers(brand, capsys):
    seed(brand, [item(dates_verified=False)])
    queue_manager.cmd_status(_ns(brand=brand))
    out = capsys.readouterr().out
    assert "DATES" in out
    assert "--force --reason" in out, (
        "an approve instruction that will fail when followed teaches that the refusal is noise"
    )


# ── publisher ─────────────────────────────────────────────────────────────────────


def test_publisher_matches_the_entry_through_the_shared_resolver(brand):
    """The publisher holds an absolute path; the queue stores a brand-relative one."""
    from pipeline.publisher import queue_entry_for

    cfg = queue_manager.load_brand(brand)
    seed(brand, [item(slug="target"), item(draft_path="staging/drafts/other-001-v1.md", slug="other")])
    found = queue_entry_for(cfg.resolve_draft_path("staging/drafts/draft-001-v1.md"), cfg)
    assert found is not None and found["slug"] == "target"


def test_publisher_returns_none_for_a_draft_that_is_not_queued(brand):
    """Absent from the record warns and proceeds — the caller owns that rule, not this
    module — so the lookup must distinguish 'not there' from 'blocked'."""
    from pipeline.publisher import queue_entry_for

    cfg = queue_manager.load_brand(brand)
    seed(brand, [item()])
    assert queue_entry_for(cfg.resolve_draft_path("staging/drafts/absent-001-v1.md"), cfg) is None
