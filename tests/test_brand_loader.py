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
