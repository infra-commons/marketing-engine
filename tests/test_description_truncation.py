"""
Unit tests for meta-description truncation (pipeline/publisher.py).

The description written here reaches the publish queue at enqueue time and, from there,
the published page's `<meta name="description">` and JSON-LD block. A bare slice at the
character limit ends mid-word, so what these assert is the boundary, not just the length.
Pure functions — no network, no filesystem.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.publisher import (  # noqa: E402
    DESCRIPTION_LIMIT,
    make_description,
    truncate_description,
)

LONG = (
    "Employers reworking their payroll for the new minimum wage are discovering that the "
    "headline hourly rate is the smallest part of the bill, because leave loading and "
    "holiday pay both compound off it every single pay run."
)


class TestTruncateDescription:
    def test_short_text_is_returned_unchanged_with_no_ellipsis(self):
        text = "A short description that fits comfortably."
        assert truncate_description(text) == text

    def test_text_exactly_at_the_limit_is_untouched(self):
        text = "x" * DESCRIPTION_LIMIT
        assert truncate_description(text) == text

    def test_long_text_fits_the_limit_and_carries_an_ellipsis(self):
        out = truncate_description(LONG)
        assert len(LONG) > DESCRIPTION_LIMIT
        assert len(out) <= DESCRIPTION_LIMIT
        assert out.endswith("…")

    def test_long_text_is_never_cut_mid_word(self):
        out = truncate_description(LONG)
        kept = out.rstrip("…")
        # Every word kept must be a whole word from the source, and the character after
        # the cut point in the original must be a boundary rather than more of a word.
        assert LONG.startswith(kept)
        assert LONG[len(kept)] == " "

    def test_trailing_punctuation_exposed_by_the_cut_is_dropped(self):
        text = "Alpha beta gamma, " + "delta " * 60
        out = truncate_description(text)
        assert not out.rstrip("…").endswith(",")
        assert not out.rstrip("…").endswith(" ")

    def test_single_token_longer_than_the_limit_still_truncates(self):
        out = truncate_description("y" * 400)
        assert len(out) <= DESCRIPTION_LIMIT
        assert out.endswith("…")

    def test_whitespace_is_normalised(self):
        assert truncate_description("  two\n\nlines   here  ") == "two lines here"

    def test_empty_input_is_empty_output(self):
        assert truncate_description("") == ""
        assert truncate_description(None) == ""


class TestMakeDescription:
    def test_brief_branch_does_not_cut_mid_word(self):
        out = make_description("unused body", {"topic_statement": LONG})
        assert len(out) <= DESCRIPTION_LIMIT
        assert LONG[len(out.rstrip("…"))] == " "

    def test_body_branch_does_not_cut_mid_word(self):
        body = "## Heading\n\n" + LONG
        out = make_description(body, None)
        assert len(out) <= DESCRIPTION_LIMIT
        assert out.endswith("…")
        assert not out.rstrip("…").endswith(" ")

    def test_short_brief_statement_gets_no_ellipsis(self):
        out = make_description("body", {"topic_statement": "A complete short sentence."})
        assert out == "A complete short sentence."
