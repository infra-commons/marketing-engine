"""
Unit tests for pipeline/brand_loader.py root resolution.

These cover the engine→consumer boundary: the engine code may run vendored at
the repo root (legacy) or as a git submodule (post-migration), and must locate
the consuming repo's brands/ config correctly in both layouts. Tests run in CI
without external dependencies.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.brand_loader import (  # noqa: E402
    ENGINE_ROOT,
    VALID_ARTICLE_TYPES,
    _validate_site_subpath,
    brands_dir,
    consumer_root,
    load_brand,
)


class TestConsumerRootResolution:
    def test_defaults_to_engine_root_when_unset(self, monkeypatch):
        # Legacy vendored layout: engine lives at the repo root, so the consumer
        # root is the engine root and no configuration is needed.
        monkeypatch.delenv("MARKETING_REPO_ROOT", raising=False)
        assert consumer_root() == ENGINE_ROOT.resolve()

    def test_env_var_overrides(self, monkeypatch, tmp_path):
        # Submodule layout: the consumer points the engine at its repo root.
        monkeypatch.setenv("MARKETING_REPO_ROOT", str(tmp_path))
        assert consumer_root() == tmp_path.resolve()

    def test_brands_dir_follows_consumer_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MARKETING_REPO_ROOT", str(tmp_path))
        assert brands_dir() == tmp_path.resolve() / "brands"


class TestLoadBrandErrors:
    def test_missing_brands_dir_hints_at_env_var(self, monkeypatch, tmp_path):
        # Point at an empty root (no brands/) — the error should tell the operator
        # how to fix a misconfigured submodule.
        monkeypatch.setenv("MARKETING_REPO_ROOT", str(tmp_path))
        with pytest.raises(FileNotFoundError) as exc:
            load_brand("cashbucket")
        assert "MARKETING_REPO_ROOT" in str(exc.value)

    def test_unknown_brand_lists_available(self, monkeypatch, tmp_path):
        # brands/ exists with one brand; asking for another lists what's there.
        (tmp_path / "brands" / "realbrand").mkdir(parents=True)
        (tmp_path / "brands" / "realbrand" / "brand.yaml").write_text("brand: realbrand\n")
        monkeypatch.setenv("MARKETING_REPO_ROOT", str(tmp_path))
        with pytest.raises(FileNotFoundError) as exc:
            load_brand("ghostbrand")
        assert "realbrand" in str(exc.value)


class TestValidateSiteSubpath:
    """Guard against path traversal in brand-config site-relative paths.

    These values are joined onto the site repo checkout, and publisher echoes
    index content back in its duplicate-title error — so a `..`/absolute value
    is a path-traversal + exfiltration vector (rolliq-com/marketing#75).
    """

    def test_accepts_plain_relative_path(self):
        assert _validate_site_subpath("articles_index", "articles.html") == "articles.html"

    def test_accepts_nested_relative_path(self):
        assert _validate_site_subpath("articles_path", "site/articles") == "site/articles"

    def test_accepts_empty_value(self):
        # Optional fields may be empty; nothing to traverse.
        assert _validate_site_subpath("assets_path", "") == ""

    def test_rejects_parent_traversal(self):
        with pytest.raises(ValueError, match="relative path within the site repo"):
            _validate_site_subpath("articles_index", "../../etc/passwd")

    def test_rejects_traversal_mid_path(self):
        with pytest.raises(ValueError):
            _validate_site_subpath("articles_index", "site/../../secret.json")

    def test_rejects_absolute_path(self):
        with pytest.raises(ValueError, match="no leading '/'"):
            _validate_site_subpath("assets_path", "/etc/passwd")


class TestEditorialConfig:
    """workflow.article_types / editorial_rules / timely — all optional, and a brand
    that sets none of them keeps the behaviour it had before they existed."""

    @staticmethod
    def _cfg(monkeypatch, **workflow):
        monkeypatch.setenv("MARKETING_REPO_ROOT", str(Path(__file__).parent / "fixtures"))
        cfg = load_brand("testbrand")
        cfg.workflow = {**cfg.workflow, **workflow}
        return cfg

    def test_unset_keys_keep_the_old_defaults(self, monkeypatch):
        cfg = self._cfg(monkeypatch)
        assert cfg.allowed_article_types == VALID_ARTICLE_TYPES
        assert cfg.allowed_article_types[0] == "news-reaction"
        assert cfg.article_types_restricted is False
        assert cfg.editorial_rules == []
        assert cfg.timely_topics is True

    def test_configured_keys_are_read(self, monkeypatch):
        cfg = self._cfg(
            monkeypatch,
            article_types=["how-to", "explainer"],
            editorial_rules=["no news", "  "],
            timely=False,
        )
        assert cfg.allowed_article_types == ("how-to", "explainer")
        assert cfg.article_types_restricted is True
        assert cfg.editorial_rules == ["no news"]
        assert cfg.timely_topics is False

    @pytest.mark.parametrize("bad", [[], "how-to", ["how-to", "listicle"]])
    def test_unusable_article_types_raise(self, monkeypatch, bad):
        with pytest.raises(ValueError, match="article_types"):
            self._cfg(monkeypatch, article_types=bad).allowed_article_types

    def test_editorial_rules_must_be_a_list_of_strings(self, monkeypatch):
        with pytest.raises(ValueError, match="editorial_rules"):
            self._cfg(monkeypatch, editorial_rules="no news").editorial_rules

    def test_quoted_timely_is_refused_not_read_as_true(self, monkeypatch):
        with pytest.raises(ValueError, match="timely"):
            self._cfg(monkeypatch, timely="false").timely_topics
