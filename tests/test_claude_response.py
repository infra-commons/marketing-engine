"""
Tests for pipeline/claude_response.py — the reply is the first text block, not content[0].
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.claude_response import NoTextBlockError, reply_text  # noqa: E402


def _response(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


THINKING = SimpleNamespace(type="thinking", thinking="let me work this out")


def test_skips_a_leading_thinking_block():
    assert reply_text(_response(THINKING, SimpleNamespace(type="text", text="reply"))) == "reply"


def test_returns_the_first_of_several_text_blocks():
    response = _response(
        SimpleNamespace(type="text", text="first"), SimpleNamespace(type="text", text="second")
    )
    assert reply_text(response) == "first"


def test_no_text_block_fails_naming_what_it_saw():
    with pytest.raises(NoTextBlockError) as exc:
        reply_text(_response(THINKING, stop_reason="max_tokens"))
    assert "thinking" in str(exc.value)
    assert "max_tokens" in str(exc.value)


def test_an_empty_response_fails():
    with pytest.raises(NoTextBlockError):
        reply_text(_response())
