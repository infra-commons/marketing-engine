"""
Unit tests for the title duplicate check in pipeline/publisher.py.

Covers `_find_duplicate_title` / `_normalise_title` — the guard that blocks
re-publishing an article whose headline already appears on the brand's articles
index. Runs in CI without external dependencies (filesystem only).
"""

import sys
from pathlib import Path
from types import SimpleNamespace

# Ensure the repo root is on sys.path so pipeline imports resolve correctly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.publisher import _find_duplicate_title, _normalise_title  # noqa: E402

ARTICLES_INDEX = "articles.html"


def _brand_cfg():
    # The dedupe helpers only read brand_cfg.articles_index.
    return SimpleNamespace(articles_index=ARTICLES_INDEX)


def _write_index(tmp_path: Path, *titles: str) -> Path:
    cards = "\n".join(f"<article><h2>{t}</h2></article>" for t in titles)
    (tmp_path / ARTICLES_INDEX).write_text(
        f"<main>{cards}</main>", encoding="utf-8"
    )
    return tmp_path


class TestNormaliseTitle:
    def test_strips_punctuation_and_lowercases(self):
        assert _normalise_title("Here's Why It Matters!") == "heres why it matters"

    def test_em_dash_is_stripped_as_punctuation(self):
        # Punctuation is removed but surrounding whitespace is left intact
        # (the helper does not collapse internal runs of spaces).
        assert _normalise_title("KiwiSaver — A Guide") == "kiwisaver  a guide"


class TestFindDuplicateTitle:
    def test_returns_existing_on_exact_match(self, tmp_path):
        site = _write_index(tmp_path, "How KiwiSaver Works")
        assert (
            _find_duplicate_title("How KiwiSaver Works", site, _brand_cfg())
            == "How KiwiSaver Works"
        )

    def test_match_ignores_punctuation_and_case(self, tmp_path):
        site = _write_index(tmp_path, "How KiwiSaver Works")
        assert (
            _find_duplicate_title("how kiwisaver works!", site, _brand_cfg())
            == "How KiwiSaver Works"
        )

    def test_returns_none_when_no_match(self, tmp_path):
        site = _write_index(tmp_path, "How KiwiSaver Works")
        assert _find_duplicate_title("Something Entirely New", site, _brand_cfg()) is None

    def test_returns_none_when_index_missing(self, tmp_path):
        assert _find_duplicate_title("Anything", tmp_path, _brand_cfg()) is None
