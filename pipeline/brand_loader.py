"""
pipeline/brand_loader.py — Brand configuration loader for multi-tenancy.

Loads brand.yaml + phrase_banks.py from brands/{brand}/ and returns a typed
BrandConfig. Used by draft_generator, publisher, and queue_manager.
"""

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml


def _validate_site_subpath(field_name: str, value: str) -> str:
    """Reject absolute paths or `..` traversal in a brand's site-relative path.

    The site-structure fields (`articles_path`, `articles_index`, `assets_path`)
    come from a brand's `brand.yaml` and are joined onto the site repo checkout
    (e.g. `site_path / brand_cfg.articles_index`). A value like `../../etc/passwd`
    would let a malicious or misconfigured brand config read or write outside the
    site repo — and `publisher._find_duplicate_title` echoes index content back in
    its duplicate-title error, which would exfiltrate the read file. Constrain
    these to safe relative subpaths.
    """
    if value and (os.path.isabs(value) or ".." in PurePosixPath(value).parts):
        raise ValueError(
            f"Brand config field '{field_name}' must be a relative path within the "
            f"site repo (no leading '/' or '..'): {value!r}"
        )
    return value

# Where the engine's own code lives (this file's parent dir's parent). The base
# phrase_banks.py module sits here; brand phrase_banks.py files import from it.
# This is always correct — vendored at the repo root, or as a git submodule.
ENGINE_ROOT = Path(__file__).parent.parent


def consumer_root() -> Path:
    """Root of the *consuming* brand repo — where the brand `brands/` config lives.

    When the engine is vendored at the repo root, this equals ENGINE_ROOT, so the
    default keeps the legacy layout working with no configuration. When the engine
    is consumed as a git submodule (at e.g. `engine/`), the submodule's own
    location is NOT the brand repo root, so the consumer must export
    MARKETING_REPO_ROOT pointing at its repo root. (The shell entrypoints
    `cd $(dirname $0)` into the engine dir, so CWD is not reliable here.)
    """
    env = os.environ.get("MARKETING_REPO_ROOT")
    if env:
        return Path(env).resolve()
    return ENGINE_ROOT.resolve()


def brands_dir() -> Path:
    """Directory holding per-brand config (`brands/`), in the consuming repo."""
    return consumer_root() / "brands"


DEFAULT_BRAND = "cashbucket"


@dataclass
class BrandConfig:
    # Identity
    brand: str
    display_name: str
    brand_dir: Path
    # Site repository
    site_repo: str
    site_local_name: str
    site_url: str
    # Secrets (env var names)
    gh_token_env: str
    unsplash_key_env: str
    # Site structure (relative to site repo root)
    articles_path: str
    articles_index: str
    assets_path: str
    articles_grid_marker: str
    # URLs
    article_url_base: str
    unsplash_utm_source: str
    # Article page CTA section
    cta_booking_url: str
    cta_contact_url: str
    cta_headline: str
    cta_body: str
    cta_btn_primary: str
    cta_btn_secondary: str
    tagline: str
    # Draft generator context
    platform_description: str
    brand_section_label: str
    # Visual
    colors: dict
    hero_gradients: dict
    article_type_labels: dict
    # Social
    social: dict

    # --- Optional, brand-opt-in (defaults preserve the legacy/rolliq behaviour) ---
    # Article-end newsletter signup CTA (empty string => no newsletter CTA rendered).
    newsletter_signup_url: str = ""
    newsletter_cta_headline: str = ""
    newsletter_cta_body: str = ""
    # Channel config dicts, consumed by brand-local tools (MailerLite/Buffer/analytics).
    # Empty dict => channel inactive for this brand.
    newsletter: dict = field(default_factory=dict)
    buffer: dict = field(default_factory=dict)
    analytics: dict = field(default_factory=dict)
    # Metrics dashboard config (consumed by pipeline.dashboard_generator). Empty dict
    # => the brand has no dashboard wired up. Keys: title, output_dir, marketing_repo,
    # staging_site_url, infra_workflows (list of [workflow_file, display_name]),
    # token_expiry (map of label -> YYYY-MM-DD).
    dashboard: dict = field(default_factory=dict)
    # Workflow config. Controls staging-dir layout + approval model so the engine
    # can serve both the dir-move workflow (review/ -> approved/, default) and the
    # status-flag workflow (single drafts/ dir + explicit `approve` command).
    #   draft_dir: name of the dir draft_generator writes to       (default "review")
    #   approval:  "dirmove" (move file review/->approved/) | "status" (flag in queue)
    workflow: dict = field(default_factory=dict)

    def __post_init__(self):
        # Guard the site-relative path fields against absolute paths / `..`
        # traversal before they are ever joined onto a site checkout.
        for _field in ("articles_path", "articles_index", "assets_path"):
            _validate_site_subpath(_field, getattr(self, _field))

    @property
    def staging_dir(self) -> Path:
        return self.brand_dir / "staging"

    @property
    def briefs_dir(self) -> Path:
        return self.staging_dir / "briefs"

    @property
    def review_dir(self) -> Path:
        return self.staging_dir / "review"

    @property
    def approved_dir(self) -> Path:
        return self.staging_dir / "approved"

    @property
    def social_dir(self) -> Path:
        return self.staging_dir / "social"

    # --- Optional staging dirs used by the status-flag workflow / brand tools ---
    @property
    def drafts_dir(self) -> Path:
        return self.staging_dir / "drafts"

    @property
    def topics_dir(self) -> Path:
        return self.staging_dir / "topics"

    @property
    def campaigns_dir(self) -> Path:
        return self.staging_dir / "campaigns"

    @property
    def approval_model(self) -> str:
        """'dirmove' (default) or 'status'."""
        return self.workflow.get("approval", "dirmove")

    @property
    def draft_output_dir(self) -> Path:
        """Dir draft_generator writes new drafts to. Defaults to review_dir for the
        dir-move workflow; status-flag brands point this at drafts/."""
        return self.staging_dir / self.workflow.get("draft_dir", "review")

    @property
    def queue_path(self) -> Path:
        return self.staging_dir / "publish_queue.json"

    def resolve_draft_path(self, relative_path: str) -> Path:
        """Resolve a queue draft_path (relative to brand dir) to an absolute Path."""
        p = Path(relative_path)
        return p if p.is_absolute() else self.brand_dir / p

    def hero_gradient(self, article_type: str) -> str:
        return self.hero_gradients.get(article_type, self.hero_gradients.get("default", ""))

    def article_type_label(self, article_type: str) -> str:
        return self.article_type_labels.get(article_type, "Article")


def load_brand(brand_slug: str) -> BrandConfig:
    """Load and return a BrandConfig for the given brand slug."""
    root = brands_dir()
    brand_dir = root / brand_slug
    yaml_path = brand_dir / "brand.yaml"
    if not yaml_path.exists():
        available = [d.name for d in root.iterdir() if d.is_dir()] if root.exists() else []
        hint = "" if root.exists() else (
            f"\nbrands/ dir not found at {root} — if the engine is a submodule, "
            f"export MARKETING_REPO_ROOT=<your marketing repo root>."
        )
        raise FileNotFoundError(
            f"Brand config not found: {yaml_path}\n"
            f"Available brands: {available}{hint}"
        )
    with yaml_path.open(encoding="utf-8") as f:
        d = yaml.safe_load(f)
    return BrandConfig(
        brand=d["brand"],
        display_name=d["display_name"],
        brand_dir=brand_dir,
        site_repo=d["site_repo"],
        site_local_name=d["site_local_name"],
        site_url=d["site_url"],
        gh_token_env=d["gh_token_env"],
        unsplash_key_env=d["unsplash_key_env"],
        articles_path=d["articles_path"],
        articles_index=d["articles_index"],
        assets_path=d["assets_path"],
        articles_grid_marker=d["articles_grid_marker"],
        article_url_base=d["article_url_base"],
        unsplash_utm_source=d["unsplash_utm_source"],
        cta_booking_url=d["cta_booking_url"],
        cta_contact_url=d["cta_contact_url"],
        cta_headline=d["cta_headline"],
        cta_body=d["cta_body"],
        cta_btn_primary=d["cta_btn_primary"],
        cta_btn_secondary=d["cta_btn_secondary"],
        tagline=d["tagline"],
        platform_description=d["platform_description"],
        brand_section_label=d["brand_section_label"],
        colors=d["colors"],
        hero_gradients=d["hero_gradients"],
        article_type_labels=d["article_type_labels"],
        social=d["social"],
        newsletter_signup_url=d.get("newsletter_signup_url", ""),
        newsletter_cta_headline=d.get("newsletter_cta_headline", ""),
        newsletter_cta_body=d.get("newsletter_cta_body", ""),
        newsletter=d.get("newsletter", {}),
        buffer=d.get("buffer", {}),
        analytics=d.get("analytics", {}),
        dashboard=d.get("dashboard", {}),
        workflow=d.get("workflow", {}),
    )


def load_phrase_banks(brand_dir: Path):
    """Dynamically load phrase_banks.py from a brand directory."""
    # The brand phrase_banks.py does `from phrase_banks import ...`, resolving to
    # the engine's base module — so ENGINE_ROOT (not the consumer root) goes on
    # the path here.
    engine_root = str(ENGINE_ROOT)
    if engine_root not in sys.path:
        sys.path.insert(0, engine_root)
    spec = importlib.util.spec_from_file_location(
        "brand_phrase_banks",
        brand_dir / "phrase_banks.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_nav_html(brand_dir: Path) -> str:
    """Load nav.html from a brand directory."""
    nav_path = brand_dir / "nav.html"
    return nav_path.read_text(encoding="utf-8").strip() if nav_path.exists() else ""


def load_footer_html(brand_dir: Path) -> str:
    """Load footer.html from a brand directory."""
    footer_path = brand_dir / "footer.html"
    return footer_path.read_text(encoding="utf-8").strip() if footer_path.exists() else ""


def load_article_cta(brand_dir: Path) -> str:
    """Load an optional article_cta.html fragment from a brand directory.

    Returns "" if absent — the publisher then renders its built-in default CTA.
    A brand supplies this fragment (a self-contained <section> + any <style>/
    <script>) to override the call-to-action block at the foot of each article
    (e.g. a newsletter signup form instead of the default demo buttons)."""
    cta_path = brand_dir / "article_cta.html"
    return cta_path.read_text(encoding="utf-8").strip() if cta_path.exists() else ""
