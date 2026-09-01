"""
pipeline/queue_policy.py — what the RECORDED state says about a queue entry.

The publish queue carries a verdict on each entry: whether the compliance gate passed,
whether the brief's figures were ever checked against a primary source, and whether the
entry has been requeued since it was last gated. Writing those down is only useful if
something reads them before the article ships, and for a long time nothing on the publish
path did — the flags were written at enqueue time and never consulted again.

This module is the single predicate the readers share. It is imported by
``queue_manager`` (approve, next, status) and by ``publisher`` (before any file is
written). Consuming repositories mirror it in their own guards, where a stale submodule
pin would otherwise disarm the check.

It deliberately imports nothing else from ``pipeline``: ``draft_generator`` already
imports ``publisher``, so a predicate hanging off ``queue_manager`` would be one import
away from a cycle.

Two rules that look like details and are not:

**Strict ``is False`` / ``is True``, never truthiness.** An entry written before a given
flag existed carries no key at all, and "unknown" must not be read as "failed". Blocking a
historical backlog retroactively is how an operator learns to click straight past a guard,
which disarms it for the case where it is right — so absence passes, loudly noted
elsewhere, rather than blocking.

**An override with no written reason is not an override.** A bare boolean records that
someone bypassed a check and nothing about why, which is worse than no record: it looks
like an audit trail without being one. ``honoured_override`` refuses to recognise a stamp
that cannot answer "who, and why".
"""

from __future__ import annotations

# workflow.approval -> the status an entry must hold before it may publish. Kept here so
# the two callers that need it cannot disagree about what "ready" means.
READY_STATUS = {"status": "approved", "dirmove": "queued"}


def ready_status(approval_model: str) -> str:
    """The publishable status for an approval model. Unknown models raise, never default.

    A typo in a brand's config silently selecting a ready status would make every check
    downstream pass entries it exists to refuse, and would do so quietly.
    """
    try:
        return READY_STATUS[approval_model]
    except KeyError:
        raise ValueError(
            f"unknown approval model {approval_model!r}; expected one of {sorted(READY_STATUS)}"
        ) from None


def recorded_blockers(item: dict) -> list[str]:
    """Reasons the recorded state says this entry must not publish. Empty means clear.

    Kept as separate sentences rather than one boolean because they fail differently and
    are fixed differently: a gate failure is fixed by editing the draft, an unverified
    brief by checking a figure against its source, a stale verdict by re-running the gate.
    A caller that can only say "blocked" cannot tell the operator which of those to do.
    """
    reasons = []

    if item.get("gate_passed") is False:
        flags = ", ".join(item.get("gate_flags") or [])
        reasons.append(f"it failed the compliance gate{f' ({flags})' if flags else ''}")

    if item.get("dates_verified") is False:
        # The one with no other line of defence. The compliance gate scores PROSE, not
        # truth: an article asserting figures the brief itself records as unverified
        # passes every rule the gate has and publishes.
        reasons.append(
            "its brief is marked dates_verified: false, so the article's figures were never "
            "checked against a primary source (the compliance gate does not check facts)"
        )

    if item.get("gate_stale") is True:
        # Set by `requeue`, which clears the recorded verdict when an entry is recovered
        # from a non-publishable status. Without this key the cleared entry would read as
        # "unknown", and unknown does not block — so requeue would have been a route
        # straight past every check here, for exactly the entries it exists to rescue.
        # Recording `gate_passed: false` instead would be a lie: the gate did not fail,
        # it did not run.
        reasons.append(
            "it was requeued and has not been re-gated since, so its recorded verdict describes "
            "a version of the draft that no longer applies"
        )

    return reasons


def honoured_override(item: dict) -> dict | None:
    """The entry's ``gate_override`` stamp, if it is well formed. Raises if it is not.

    Shape: ``{by, reason, at, blockers}``. Raising rather than ignoring a malformed stamp
    is deliberate — silently disregarding it would turn a bypass someone attempted into a
    refusal they never saw explained, and silently honouring it would accept a bypass
    nobody can account for.
    """
    stamp = item.get("gate_override")
    if stamp is None:
        return None

    subject = item.get("slug") or item.get("draft_path") or "<unnamed entry>"
    if not isinstance(stamp, dict):
        raise ValueError(f"gate_override on {subject!r} is not an object")
    if not str(stamp.get("by", "")).strip():
        raise ValueError(f"gate_override on {subject!r} records no author ('by')")
    if not str(stamp.get("reason", "")).strip():
        raise ValueError(
            f"gate_override on {subject!r} records no written reason; an override with no "
            "reason is not a record of a decision"
        )
    return stamp


def describe(item: dict) -> str:
    """A short verdict tag for a status listing: 'ok', 'GATE', 'DATES', 'STALE', '-'."""
    if item.get("gate_passed") is False:
        return "GATE"
    if item.get("dates_verified") is False:
        return "DATES"
    if item.get("gate_stale") is True:
        return "STALE"
    if "gate_passed" not in item:
        return "-"
    return "ok"
