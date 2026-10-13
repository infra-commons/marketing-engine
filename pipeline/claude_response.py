"""
pipeline/claude_response.py — read the reply out of a Messages API response.

`response.content[0].text` assumes the reply is the first block. With adaptive thinking on
(the default on current Sonnet/Opus models) the first block is a thinking block, so that
read either raises AttributeError or returns the model's reasoning as if it were the
article. This picks the first text block instead, and says plainly when there is none.

Imports nothing else from ``pipeline``, so any module can use it without a cycle.
"""

from __future__ import annotations


class NoTextBlockError(RuntimeError):
    """The response carried no text block — usually thinking spent the whole max_tokens."""


def reply_text(response) -> str:
    """The text of the first ``type == "text"`` block. Raises NoTextBlockError if none.

    A block with no ``type`` attribute counts as text; every real SDK block carries one.
    """
    for block in response.content:
        if getattr(block, "type", "text") == "text":
            return block.text
    types = [getattr(b, "type", "?") for b in response.content]
    raise NoTextBlockError(
        f"model response has no text block (blocks: {types or 'none'}, "
        f"stop_reason={getattr(response, 'stop_reason', None)!r}); if stop_reason is "
        "max_tokens, thinking used the whole budget before any reply was written"
    )
