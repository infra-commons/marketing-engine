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
    _looks_hard_sliced,
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


class TestMakeDescriptionSource:
    """Which text the description is derived from, not just how it is cut.

    `make_description` collapsed all whitespace before testing for a paragraph break, so
    the break could never be found and the description was the first 300 flattened
    characters of the article rather than its first paragraph.
    """

    BODY = (
        "## Why this matters\n\n"
        "- a bullet that is not the article\n\n"
        "The opening paragraph is the one a search result should show.\n\n"
        "A second paragraph that belongs to no description."
    )

    def test_the_first_prose_paragraph_is_used(self):
        out = make_description(self.BODY, None)
        assert out == "The opening paragraph is the one a search result should show."

    def test_a_leading_heading_is_not_part_of_the_description(self):
        assert "Why this matters" not in make_description(self.BODY, None)

    def test_a_leading_bullet_is_not_part_of_the_description(self):
        assert "a bullet" not in make_description(self.BODY, None)

    def test_a_later_paragraph_is_not_pulled_in(self):
        assert "second paragraph" not in make_description(self.BODY, None)

    def test_a_body_of_only_headings_still_returns_something(self):
        # No prose to prefer — fall back rather than return an empty description.
        assert make_description("## Only a heading\n", None).strip() == "Only a heading"

    def test_paragraph_longer_than_the_limit_is_still_cut_on_a_boundary(self):
        out = make_description(LONG + "\n\nA second paragraph.", None)
        assert len(out) <= DESCRIPTION_LIMIT
        assert out.endswith("…")
        assert LONG[len(out.rstrip("…"))] == " "


class TestLegacyHardSliceDetection:
    """The signature of the old bare slice, which truncating cannot repair."""

    def test_a_value_at_exactly_the_limit_with_no_ellipsis_is_flagged(self):
        assert _looks_hard_sliced("x" * DESCRIPTION_LIMIT) is True

    def test_truncating_a_legacy_value_leaves_it_unchanged(self):
        # Which is the whole reason the check exists: the damaged string is within the
        # limit, so `truncate_description` has nothing to do and reports nothing.
        legacy = "x" * DESCRIPTION_LIMIT
        assert truncate_description(legacy) == legacy

    def test_a_properly_truncated_value_is_not_flagged(self):
        assert _looks_hard_sliced(truncate_description(LONG)) is False

    def test_a_short_value_is_not_flagged(self):
        assert _looks_hard_sliced("A complete short description.") is False

    def test_empty_and_none_are_not_flagged(self):
        assert _looks_hard_sliced("") is False
        assert _looks_hard_sliced(None) is False
