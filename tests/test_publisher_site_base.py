"""
The publish branch must be rooted at origin/<base>, whatever the site checkout has out.

`git_push_site` used to `checkout -b` from the current ref, so a site checkout left on a
staging branch shipped that branch's unrelated work to prod. These tests run real git
against a local bare "origin" (no network) with the checkout deliberately on a non-base
branch, and assert the commit that would be pushed sits directly on origin/<base>.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure the repo root is on sys.path so pipeline imports resolve correctly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import publisher  # noqa: E402
from pipeline.brand_loader import BrandConfig  # noqa: E402

SLUG = "gst-float-nz-sme"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", f"add {name}")


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A clone of a bare origin with `main` and `live`, checked out on local `staging`."""
    monkeypatch.setattr(publisher, "_get_gh_token", lambda brand_cfg: "")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(origin), str(seed))
    _git(seed, "checkout", "-b", "main")
    _commit(seed, "articles.html", "<main></main>")
    _git(seed, "push", "origin", "main")
    _git(seed, "checkout", "-b", "live")
    _commit(seed, "live-only.html", "live")
    _git(seed, "push", "origin", "live")

    clone = tmp_path / "site"
    _git(tmp_path, "clone", "-b", "main", str(origin), str(clone))
    _git(clone, "checkout", "-b", "staging")
    _commit(clone, "staging-only.html", "unreleased staging work")
    return clone


def _cfg(**workflow):
    return SimpleNamespace(
        brand="testbrand",
        site_repo="example/site",
        site_url="https://example.com",
        articles_path="articles",
        articles_index="articles.html",
        assets_path="assets",
        workflow=workflow,
        site_base_branch=workflow.get("site_base_branch", "main"),
    )


def _publish_dry_run(site: Path, cfg) -> None:
    publisher.checkout_site_base(site, SLUG, cfg)
    (site / "articles").mkdir(exist_ok=True)
    (site / "articles" / f"{SLUG}.html").write_text("<article/>", encoding="utf-8")
    publisher.git_push_site(site, SLUG, cfg, dry_run=True)


class TestPublishBranchRoot:
    def test_branch_from_staging_checkout_is_rooted_at_origin_main(self, site):
        _publish_dry_run(site, _cfg())

        assert _git(site, "branch", "--show-current") == f"publish/{SLUG}"
        # Dry run soft-resets its commit, so HEAD is the branch point itself.
        assert _git(site, "rev-parse", "HEAD") == _git(site, "rev-parse", "origin/main")
        tree = _git(site, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        assert "staging-only.html" not in tree
        assert not (site / "staging-only.html").exists()
        assert f"articles/{SLUG}.html" in _git(site, "diff", "--cached", "--name-only")

    def test_configured_base_branch_is_used(self, site):
        _publish_dry_run(site, _cfg(site_base_branch="live"))

        assert _git(site, "rev-parse", "HEAD") == _git(site, "rev-parse", "origin/live")
        assert (site / "live-only.html").exists()
        assert not (site / "staging-only.html").exists()

    def test_dirty_tracked_file_is_refused(self, site):
        (site / "articles.html").write_text("<main>local edit</main>", encoding="utf-8")
        with pytest.raises(RuntimeError, match="uncommitted changes"):
            publisher.checkout_site_base(site, SLUG, _cfg())
        assert _git(site, "branch", "--show-current") == "staging"


class TestMergeFailureMessage:
    def test_prints_gh_reason_not_a_fixed_staging_excuse(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(publisher, "_get_gh_token", lambda brand_cfg: "")

        def fake_run(cmd, cwd, check=True, env=None):
            if cmd[:3] == ["gh", "pr", "merge"]:
                return subprocess.CompletedProcess(cmd, 1, "", "auto-merge is not allowed for this repository\n")
            out = "articles/x.html\n" if cmd[:3] == ["git", "diff", "--cached"] else ""
            return subprocess.CompletedProcess(cmd, 0, out, "")

        monkeypatch.setattr(publisher, "_run", fake_run)
        publisher.git_push_site(tmp_path, SLUG, _cfg(), dry_run=False)

        out = capsys.readouterr().out
        assert "auto-merge is not allowed for this repository" in out
        assert "staging" not in out


class TestSiteBaseBranchConfig:
    def _brand(self, workflow):
        cfg = BrandConfig.__new__(BrandConfig)
        cfg.brand, cfg.workflow = "testbrand", workflow
        return cfg

    def test_defaults_to_main(self):
        assert self._brand({}).site_base_branch == "main"

    def test_reads_configured_value(self):
        assert self._brand({"site_base_branch": " live "}).site_base_branch == "live"

    @pytest.mark.parametrize("bad", ["", "   ", None, 3, ["main"]])
    def test_rejects_unusable_value(self, bad):
        with pytest.raises(ValueError, match="site_base_branch"):
            self._brand({"site_base_branch": bad}).site_base_branch
