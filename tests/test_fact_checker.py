"""
Tests for how pipeline/fact_checker.py reads the model's report.

Two failures discarded good drafts. The model listed reasoning that retracted itself
("No hard arithmetic errors are present") in errors[], which counted as an error; and it
wrote prose before the JSON, which read as "unparseable". Either way check() failed and
draft_generator exited before writing the draft.

These pin the outcome: a retracted item does not fail the check, wrapped JSON parses, and
anything the model did not explicitly clear still fails closed.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import fact_checker  # noqa: E402

BRIEF = {"topic_statement": "OCR cuts", "key_facts": ["OCR is 2.25%"]}
REAL_ERROR = "Says OCR fell 325bp from 5.50% to 3.25%; it should be 2.25%."


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _run(monkeypatch, blocks, stop_reason="end_turn"):
    response = SimpleNamespace(content=blocks, stop_reason=stop_reason)
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: response))
    monkeypatch.setattr(fact_checker.anthropic, "Anthropic", lambda **_: client)
    return fact_checker.check("draft", BRIEF, api_key="test")


def _report(errors, warnings=()):
    return json.dumps({"errors": list(errors), "warnings": list(warnings)})


def test_a_retracted_item_does_not_fail_the_check(monkeypatch):
    report = _report([{"description": "No hard arithmetic errors are present", "is_error": False}])
    result = _run(monkeypatch, [_text(report)])
    assert result.passed
    assert result.flags == []


def test_a_confirmed_error_fails_and_keeps_its_description(monkeypatch):
    report = _report(
        [
            {"description": REAL_ERROR, "is_error": True},
            {"description": "Checked the 2.25% figure; it is correct", "is_error": False},
        ]
    )
    result = _run(monkeypatch, [_text(report)])
    assert not result.passed
    assert result.flags == [REAL_ERROR]


def test_prose_before_the_json_parses(monkeypatch):
    raw = "Let me check the arithmetic first. 5.50 - 3.25 = 2.25.\n\n" + _report([], ["w"])
    result = _run(monkeypatch, [_text(raw)])
    assert result.passed
    assert result.warnings == ["w"]


def test_fenced_json_parses(monkeypatch):
    result = _run(monkeypatch, [_text("Here is the report:\n```json\n" + _report([]) + "\n```")])
    assert result.passed


def test_a_leading_thinking_block_is_skipped(monkeypatch):
    blocks = [SimpleNamespace(type="thinking", thinking="{not json"), _text(_report([]))]
    assert _run(monkeypatch, blocks).passed


@pytest.mark.parametrize(
    "item",
    [REAL_ERROR, {"description": REAL_ERROR}],
    ids=["bare-string", "no-is_error-flag"],
)
def test_an_item_not_explicitly_cleared_still_fails(monkeypatch, item):
    result = _run(monkeypatch, [_text(_report([item]))])
    assert not result.passed
    assert result.flags == [REAL_ERROR]


def test_unparseable_text_fails_closed(monkeypatch):
    result = _run(monkeypatch, [_text("I could not complete the check.")])
    assert not result.passed
    assert "unparseable" in result.flags[0]


def test_a_truncated_response_fails_and_says_so(monkeypatch):
    result = _run(monkeypatch, [_text('{"errors": [')], stop_reason="max_tokens")
    assert not result.passed
    assert "truncated" in result.flags[0]
