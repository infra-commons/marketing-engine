"""
pipeline/rebake_chrome.py — Content Engine

Re-bake the shared chrome (nav + footer) into ALREADY-published article pages.

`publisher.build_article_html()` inlines `brands/<brand>/nav.html` and
`footer.html` verbatim into each article at publish time — the article does not
reference them at runtime. So a chrome/brand change only reaches articles
published *after* it; existing ones strand with the old chrome. This tool walks a
site repo's `articles/*.html` and rewrites the inlined `<header class="site-header">`
block and the trailing footer block to the brand's current canonical chrome.

Properties:
  - Idempotent: re-running on already-current files makes no changes, exits 0.
  - Chrome-only: body content, hero images, publish dates, inline CSS untouched.
  - Skip-and-report: a file whose header/footer block count != 1 is skipped, not
    corrupted.
  - Dry-run by default: prints a per-file summary + unified diff, writes nothing.
    `--write` is required to modify files.
  - Brand-agnostic: reuses publisher's brand + site-path resolution.

Usage:
    python3 -m pipeline.rebake_chrome --brand rolliq              # dry-run
    python3 -m pipeline.rebake_chrome --brand rolliq --write      # apply
    python3 -m pipeline.rebake_chrome --brand rolliq --site-path ../website --write
"""

import argparse
import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Add repo root so local modules import cleanly
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.brand_loader import (
    DEFAULT_BRAND,
    consumer_root,
    load_brand,
    load_footer_html,
    load_nav_html,
)

# The header block is inserted verbatim by the publisher as a clean delimited
# region, so a non-greedy DOTALL swap is exact. See publisher.build_article_html.
HEADER_RE = re.compile(r'<header class="site-header">.*?</header>', re.DOTALL)


def _footer_pattern(footer_html: str) -> re.Pattern:
    """Pattern matching the inlined footer region in a published article.

    The publisher inlines the canonical footer_html (tagline strip + <footer> +
    burger script) verbatim immediately before `</body>`. We anchor on the first
    opening tag of the canonical footer_html (e.g. `<div class="tagline-strip">`)
    and consume up to — but not including — `</body>`. Deriving the start anchor
    from the canonical footer keeps this brand-agnostic.
    """
    m = re.match(r"\s*(<[^>]+>)", footer_html)
    if not m:
        raise ValueError("Canonical footer_html does not start with an HTML tag.")
    start = m.group(1)
    return re.compile(re.escape(start) + r".*?(?=</body>)", re.DOTALL)


@dataclass
class RebakeResult:
    header_count: int
    footer_count: int
    changed: bool
    skipped: bool
    new_html: str

    @property
    def reason(self) -> str:
        if self.header_count != 1:
            return f"header block count = {self.header_count} (expected 1)"
        if self.footer_count != 1:
            return f"footer block count = {self.footer_count} (expected 1)"
        return ""


def rebake_html(html: str, nav_html: str, footer_html: str) -> RebakeResult:
    """Compute the re-baked HTML for a single article. Pure: writes nothing.

    A file is *skipped* (and left byte-for-byte unchanged) unless it contains
    exactly one header block and exactly one footer block — never partially
    rewritten.
    """
    footer_re = _footer_pattern(footer_html)
    header_count = len(HEADER_RE.findall(html))
    footer_count = len(footer_re.findall(html))

    if header_count != 1 or footer_count != 1:
        return RebakeResult(header_count, footer_count, changed=False, skipped=True, new_html=html)

    # Function replacements avoid re backreference interpretation of the chrome.
    new_html = HEADER_RE.sub(lambda _m: nav_html, html)
    new_html = footer_re.sub(lambda _m: footer_html + "\n", new_html)

    return RebakeResult(
        header_count,
        footer_count,
        changed=(new_html != html),
        skipped=False,
        new_html=new_html,
    )


@dataclass
class FileResult:
    path: Path
    result: RebakeResult
    written: bool


def rebake_dir(articles_dir: Path, nav_html: str, footer_html: str, write: bool) -> list[FileResult]:
    """Re-bake every `*.html` under articles_dir. Returns one FileResult per file."""
    out: list[FileResult] = []
    for path in sorted(articles_dir.glob("*.html")):
        html = path.read_text(encoding="utf-8")
        result = rebake_html(html, nav_html, footer_html)
        written = False
        if write and result.changed and not result.skipped:
            path.write_text(result.new_html, encoding="utf-8")
            written = True
        out.append(FileResult(path=path, result=result, written=written))
    return out


def _print_summary(file_results: list[FileResult], write: bool) -> None:
    changed = skipped = unchanged = 0
    for fr in file_results:
        r = fr.result
        name = fr.path.name
        if r.skipped:
            skipped += 1
            print(f"  SKIP      {name}  — {r.reason}")
        elif r.changed:
            changed += 1
            verb = "WROTE" if fr.written else "would update"
            print(f"  {verb:<9} {name}")
            if not write:
                diff = difflib.unified_diff(
                    fr.path.read_text(encoding="utf-8").splitlines(),
                    r.new_html.splitlines(),
                    fromfile=f"{name} (current)",
                    tofile=f"{name} (re-baked)",
                    lineterm="",
                )
                for line in diff:
                    print(f"      {line}")
        else:
            unchanged += 1
            print(f"  unchanged {name}")
    mode = "WRITE" if write else "DRY-RUN (no files written; pass --write to apply)"
    print(f"\n{mode}: {changed} changed, {unchanged} unchanged, {skipped} skipped.")


def main(argv: list[str] | None = None) -> int:
    repo_root = consumer_root()

    parser = argparse.ArgumentParser(
        description="Re-bake shared chrome (nav + footer) into already-published articles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--brand", default=DEFAULT_BRAND, help=f"Brand workspace (default: {DEFAULT_BRAND})")
    parser.add_argument(
        "--site-path",
        default="",
        help="Path to site repo (default: ../{brand.site_local_name} relative to content-engine)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write changes. Without this flag the tool is dry-run and writes nothing.",
    )
    args = parser.parse_args(argv)

    brand_cfg = load_brand(args.brand)
    nav_html = load_nav_html(brand_cfg.brand_dir)
    footer_html = load_footer_html(brand_cfg.brand_dir)
    if not nav_html or not footer_html:
        print(
            f"ERROR: brand '{args.brand}' is missing nav.html and/or footer.html in {brand_cfg.brand_dir}",
            file=sys.stderr,
        )
        return 1

    if args.site_path:
        site_path = Path(args.site_path)
    else:
        site_path = repo_root.parent / brand_cfg.site_local_name
    articles_dir = site_path / brand_cfg.articles_path
    if not articles_dir.exists():
        print(f"ERROR: articles dir not found: {articles_dir}", file=sys.stderr)
        print(f"  Checkout {brand_cfg.site_repo} alongside content-engine, or pass --site-path.", file=sys.stderr)
        return 1

    print(f"Re-baking chrome for brand '{brand_cfg.display_name}' in {articles_dir}\n")
    file_results = rebake_dir(articles_dir, nav_html, footer_html, write=args.write)
    if not file_results:
        print("  (no articles found)")
        return 0
    _print_summary(file_results, write=args.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
