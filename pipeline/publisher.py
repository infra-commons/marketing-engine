"""
pipeline/publisher.py — Content Engine

Publishes an approved draft to a brand's site repository.

Flow:
  1. Load brand config
  2. Parse draft markdown → extract title + body
  3. Load matching brief JSON (article_type, topic metadata)
  4. Convert markdown → HTML
  5. Write site/articles/{slug}.html to {brand}-site
  6. Prepend new card to site/articles.html
  7. Generate social variants → brands/{brand}/staging/social/
  8. Commit + push to the brand's site repo

Usage:
    python3 -m pipeline.publisher staging/approved/draft-003-v1.md --brand cashbucket
    python3 -m pipeline.publisher staging/approved/draft-003-v1.md --brand cashbucket \\
        --slug nz-minimum-wage-true-cost \\
        --description "What the $23.50 minimum wage actually costs NZ SMEs" \\
        --publish-date 2026-05-27 \\
        --dry-run
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

# Add repo root so local modules import cleanly
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.brand_loader import DEFAULT_BRAND, BrandConfig, load_brand, load_footer_html, load_nav_html
from pipeline.compliance_gate import check as compliance_check

# ─────────────────────────────────────────────────────────────────────────────
# Unsplash hero image
# ─────────────────────────────────────────────────────────────────────────────

_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "is", "it", "its", "what", "how",
    "your", "you", "why", "when", "who", "which", "that", "this", "as",
    "are", "was", "be", "been", "have", "has", "do", "does", "will", "can",
    "could", "should", "would", "not", "no", "if", "than", "then", "out",
    "into", "just", "more", "about", "after", "means", "true", "real",
    "here", "already", "either", "decides", "tomorrow", "counted",
}

_QUERY_REPLACEMENTS = {
    "rbnz": "central bank",
    "ocr":  "interest rate",
    "kiwisaver": "retirement savings",
    "nzd": "new zealand dollar",
    "gst": "tax invoice",
    "ird": "tax office",
    "acc": "workplace",
}

UNSPLASH_API_BASE = "https://api.unsplash.com"


def _load_used_photo_ids(brand_dir: Path) -> set[str]:
    path = brand_dir / "unsplash-used.json"
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def _save_used_photo_id(brand_dir: Path, photo_id: str) -> None:
    path = brand_dir / "unsplash-used.json"
    ids = _load_used_photo_ids(brand_dir)
    ids.add(photo_id)
    with path.open("w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)


def _unsplash_query(title: str, article_type: str) -> str:
    normalised = re.sub(r"[^\w\s]", " ", title.lower())
    for jargon, replacement in _QUERY_REPLACEMENTS.items():
        normalised = re.sub(rf"\b{jargon}\b", replacement, normalised)
    words = normalised.split()
    keywords = [w for w in words if w not in _STOP_WORDS and len(w) > 3][:4]
    if not keywords:
        keywords = ["business", "finance"]
    if not any(w in ("finance", "money", "business", "office", "bank", "savings", "rate") for w in keywords):
        keywords.append("business")
    return " ".join(keywords)


def fetch_unsplash_image(
    title: str,
    article_type: str,
    slug: str,
    site_path: Path,
    access_key: str,
    brand_cfg: BrandConfig,
) -> tuple[str | None, str | None]:
    """
    Fetch a relevant stock photo from Unsplash and save it to the site assets.

    Returns:
        (img_src, credit_html) or (None, None) on failure.
    """
    if not access_key:
        return None, None

    query = _unsplash_query(title, article_type)
    params = urllib.parse.urlencode({
        "query": query,
        "orientation": "landscape",
        "per_page": "10",
        "content_filter": "high",
        "client_id": access_key,
    })
    api_url = f"{UNSPLASH_API_BASE}/search/photos?{params}"

    try:
        with urllib.request.urlopen(api_url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print(f"  ⚠  Unsplash API error ({exc}) — publishing without hero image.")
        return None, None

    results = data.get("results", [])
    if not results:
        print("  ⚠  Unsplash returned no results — publishing without hero image.")
        return None, None

    used_ids = _load_used_photo_ids(brand_cfg.brand_dir)
    photo = next((r for r in results if r.get("id") not in used_ids), None)
    if photo is None:
        print("  ⚠  All Unsplash results already used by this brand — reusing first result.")
        photo = results[0]

    download_url = photo.get("urls", {}).get("regular", "")
    if not download_url:
        print("  ⚠  Unsplash returned no image URL — publishing without hero image.")
        return None, None

    # Trigger the required download event (Unsplash API guidelines)
    try:
        dl_event = photo.get("links", {}).get("download_location", "")
        if dl_event:
            req = urllib.request.Request(
                f"{dl_event}?client_id={access_key}",
                headers={"Accept-Version": "v1"},
            )
            urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

    dest_path = site_path / brand_cfg.assets_path / f"art-{slug}.jpg"
    try:
        urllib.request.urlretrieve(download_url, dest_path)
    except urllib.error.URLError as exc:
        print(f"  ⚠  Could not download Unsplash image ({exc}) — publishing without hero image.")
        return None, None

    _save_used_photo_id(brand_cfg.brand_dir, photo.get("id", ""))

    utm = brand_cfg.unsplash_utm_source
    photographer = photo.get("user", {}).get("name", "Unsplash")
    photographer_url = photo.get("user", {}).get("links", {}).get("html", "https://unsplash.com")
    credit_html = (
        f'<p class="art-img-credit">Photo by '
        f'<a href="{photographer_url}?utm_source={utm}&utm_medium=referral" '
        f'target="_blank" rel="noopener noreferrer">{photographer}</a> on '
        f'<a href="https://unsplash.com?utm_source={utm}&utm_medium=referral" '
        f'target="_blank" rel="noopener noreferrer">Unsplash</a></p>'
    )

    img_src = f"/assets/art-{slug}.jpg"
    print(f"  ✓ Hero image → art-{slug}.jpg  (query: '{query}', photo: {photographer})")
    return img_src, credit_html


# ─────────────────────────────────────────────────────────────────────────────
# Markdown → HTML
# ─────────────────────────────────────────────────────────────────────────────

def _inline(text: str) -> str:
    text = re.sub(
        r'\[([^\]]+)\]\(([^)]+)\)',
        r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>',
        text,
    )
    # Strip bold to plain text — mid-paragraph bold reads as an AI formatting tell.
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*<>]+)\*', r'<em>\1</em>', text)
    return text


def md_to_html(body: str) -> tuple[str, str]:
    """
    Convert draft markdown body to HTML.

    `---` lines are thematic section dividers and are dropped (headings carry
    the structure). A trailing italic block after the FINAL `---` (e.g.
    *Sources: …*) is treated as the citations footnote; everything else stays
    in the article body.

    Returns:
        (main_html, citations_html)
    """
    lines = body.split("\n")

    # Optional trailing citations block: an italic paragraph after the last
    # `---`. Anything else following a `---` stays in the main flow as a
    # section break, so divider rules don't swallow the article body.
    citations_html = ""
    sep_indices = [idx for idx, ln in enumerate(lines) if ln.strip() == "---"]
    if sep_indices:
        tail = [ln.strip() for ln in lines[sep_indices[-1] + 1:] if ln.strip()]
        tail_text = " ".join(tail)
        if len(tail_text) > 1 and tail_text.startswith("*") and tail_text.endswith("*"):
            cite_text = tail_text[1:-1].strip()
            if cite_text:
                citations_html = f'<p class="art-citations">{_inline(cite_text)}</p>'
            lines = lines[:sep_indices[-1]]

    main_lines = lines

    html_parts: list[str] = []
    i = 0

    while i < len(main_lines):
        line = main_lines[i]
        s = line.strip()

        if not s:
            i += 1
            continue

        if s == "---":
            # Thematic section divider — headings already carry the structure,
            # so drop it rather than stacking a rule before every <h2>.
            i += 1
            continue

        if s.startswith("## "):
            html_parts.append(f"<h2>{_inline(s[3:])}</h2>")
            i += 1
            continue

        if s.startswith("### "):
            html_parts.append(f"<h3>{_inline(s[4:])}</h3>")
            i += 1
            continue

        if re.match(r"^[-*] ", s):
            items: list[str] = []
            while i < len(main_lines) and re.match(r"^[-*] ", main_lines[i].strip()):
                item_text = re.sub(r"^[-*] ", "", main_lines[i].strip())
                items.append(f"  <li>{_inline(item_text)}</li>")
                i += 1
            html_parts.append("<ul>\n" + "\n".join(items) + "\n</ul>")
            continue

        if re.match(r"^\d+\. ", s):
            items = []
            while i < len(main_lines) and re.match(r"^\d+\. ", main_lines[i].strip()):
                item_text = re.sub(r"^\d+\. ", "", main_lines[i].strip())
                items.append(f"  <li>{_inline(item_text)}</li>")
                i += 1
            html_parts.append("<ol>\n" + "\n".join(items) + "\n</ol>")
            continue

        para_lines: list[str] = []
        while i < len(main_lines):
            cur = main_lines[i].strip()
            if not cur or cur == "---":
                break
            if cur.startswith(("## ", "### ")) or re.match(r"^[-*] ", cur) or re.match(r"^\d+\. ", cur):
                break
            para_lines.append(cur)
            i += 1
        if para_lines:
            html_parts.append(f"<p>{_inline(' '.join(para_lines))}</p>")

    return "\n\n".join(html_parts), citations_html


# ─────────────────────────────────────────────────────────────────────────────
# Draft parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_draft(draft_path: Path) -> tuple[str, str]:
    text = draft_path.read_text(encoding="utf-8")
    lines = text.split("\n")

    title = ""
    body_start = 0

    for idx, line in enumerate(lines):
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            body_start = idx + 1
        elif title and line.strip():
            body_start = idx
            break

    body = "\n".join(lines[body_start:]).strip()
    return title, body


def load_brief(draft_path: Path, brand_cfg: BrandConfig) -> dict | None:
    """Load the brief JSON corresponding to a draft (by numeric ID in filename)."""
    m = re.search(r"draft-(\d+)-", draft_path.name)
    if not m:
        return None
    brief_path = brand_cfg.briefs_dir / f"brief-{m.group(1)}.json"
    if not brief_path.exists():
        return None
    with brief_path.open(encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Slug + description helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_slug(title: str) -> str:
    slug = title.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    slug = slug[:80].rstrip("-")
    return slug


def make_description(body: str, brief: dict | None) -> str:
    if brief and brief.get("topic_statement"):
        raw = brief["topic_statement"]
        return raw[:155].rstrip() + ("…" if len(raw) > 155 else "")
    plain = re.sub(r"[*#`\[\]()_]", "", body)
    plain = re.sub(r"\s+", " ", plain).strip()
    first_para = plain.split("\n\n")[0] if "\n\n" in plain else plain[:300]
    first_para = first_para.strip()
    return first_para[:155].rstrip() + ("…" if len(first_para) > 155 else "")


def read_time(text: str) -> int:
    return max(1, round(len(text.split()) / 200))


# ─────────────────────────────────────────────────────────────────────────────
# HTML generation — article page
# ─────────────────────────────────────────────────────────────────────────────

def build_article_html(
    title: str,
    slug: str,
    body_html: str,
    citations_html: str,
    article_type: str,
    description: str,
    pub_date: str,
    mins: int,
    brand_cfg: BrandConfig,
    nav_html: str,
    footer_html: str,
    hero_img_path: str | None = None,
    credit_html: str | None = None,
) -> str:
    """Generate the full article HTML page."""
    label = brand_cfg.article_type_label(article_type)
    gradient = brand_cfg.hero_gradient(article_type)
    c = brand_cfg.colors

    title_esc = title.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    desc_esc = description.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    title_attr = title.replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")

    canonical = f"{brand_cfg.article_url_base}/{slug}.html"

    return f"""<!DOCTYPE html>
<html lang="en-NZ">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title_esc} | {brand_cfg.display_name}</title>
  <meta name="description" content="{desc_esc}" />
  <link rel="canonical" href="{canonical}" />
  <link rel="icon" href="/assets/favicon.png" sizes="32x32" />
  <link rel="apple-touch-icon" href="/assets/favicon.png" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Roboto:wght@400;500&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --teal: {c['primary']}; --teal-dark: {c['primary_dark']}; --red: {c.get('accent', '#b32317')};
      --text: {c.get('text', '#353030')}; --heading: {c['heading']}; --gray-bg: {c.get('gray_bg', '#F2F7F8')}; --max-w: 1140px;
    }}
    body {{ font-family: 'Roboto', sans-serif; color: var(--text); font-size: 16px; line-height: 1.7; }}
    h1,h2,h3,h4,nav,button {{ font-family: 'Inter', sans-serif; }}
    a {{ text-decoration: none; color: inherit; }}
    img {{ max-width: 100%; height: auto; display: block; }}

    /* NAV */
    .site-header {{ background: #fff; box-shadow: 0 2px 8px rgba(0,0,0,.07); position: sticky; top: 0; z-index: 200; }}
    .nav-inner {{ max-width: var(--max-w); margin: 0 auto; padding: 0 20px; height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 24px; }}
    .nav-logo img {{ height: 38px; width: auto; display: block; }}
    .nav-links {{ display: flex; align-items: center; gap: 24px; list-style: none; font-size: 14px; font-weight: 500; font-family: 'Inter', sans-serif; }}
    .nav-links a {{ color: var(--heading); transition: color .18s; }}
    .nav-links a:hover {{ color: var(--teal); }}
    .nav-links a.active {{ color: var(--teal); border-bottom: 2px solid var(--teal); padding-bottom: 2px; }}
    .has-drop {{ position: relative; }}
    .has-drop .drop {{ display: none; position: absolute; top: 100%; padding-top: 0; left: 0; background: #fff; border: 1px solid #e4e4e4; border-radius: 6px; min-width: 170px; box-shadow: 0 4px 14px rgba(0,0,0,.1); padding: 6px 0; }}
    .has-drop:hover .drop {{ display: block; }}
    .drop a {{ display: block; padding: 9px 16px; font-size: 13px; }}
    .drop a:hover {{ background: #f5f5f5; color: var(--teal); }}
    .nav-cta {{ background: var(--teal); color: #fff !important; padding: 9px 18px; border-radius: 5px; font-weight: 600; font-size: 13px; letter-spacing: .5px; }}
    .nav-cta:hover {{ opacity: .88; }}
    .nav-burger {{ display: none; background: none; border: none; cursor: pointer; padding: 4px; }}
    .nav-burger span {{ display: block; width: 22px; height: 2px; background: var(--heading); margin: 5px 0; border-radius: 2px; }}

    /* ARTICLE HERO */
    .art-hero {{ background: {gradient}; padding: 56px 20px; }}
    .art-hero-inner {{ max-width: 820px; margin: 0 auto; }}
    .art-tag {{ display: inline-block; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 16px; padding: 4px 12px; border-radius: 20px; background: rgba(255,255,255,.18); color: #fff; font-family: 'Inter', sans-serif; }}
    .art-hero h1 {{ font-size: 38px; font-weight: 700; color: #fff; line-height: 1.2; margin-bottom: 16px; }}
    .art-meta {{ font-size: 13px; color: rgba(255,255,255,.7); font-family: 'Inter', sans-serif; }}

    /* ARTICLE BODY */
    .art-body {{ background: #fff; padding: 60px 20px 80px; }}
    .art-body-inner {{ max-width: 820px; margin: 0 auto; }}
    .art-body-inner .hero-img {{ width: 100%; border-radius: 10px; margin-bottom: 40px; }}
    .art-img-credit {{ font-size: 11px; font-style: italic; color: #999; text-align: right; margin-top: -30px; margin-bottom: 44px; }}
    .art-img-credit a {{ color: #999; text-decoration: underline; }}
    .art-body-inner p {{ font-size: 16px; color: #444; line-height: 1.8; margin-bottom: 20px; }}
    .art-body-inner h2 {{ font-size: 26px; font-weight: 700; color: var(--heading); margin: 40px 0 16px; line-height: 1.25; }}
    .art-body-inner h3 {{ font-size: 20px; font-weight: 700; color: var(--heading); margin: 32px 0 12px; line-height: 1.3; }}
    .art-body-inner ul, .art-body-inner ol {{ padding-left: 24px; margin-bottom: 20px; }}
    .art-body-inner li {{ font-size: 16px; color: #444; line-height: 1.75; margin-bottom: 8px; }}
    .art-body-inner strong {{ color: var(--heading); font-weight: 600; }}
    .art-body-inner a {{ color: var(--teal); text-decoration: underline; }}
    .art-body-inner a:hover {{ color: var(--teal-dark); }}
    .art-body-inner blockquote {{ border-left: 4px solid var(--teal); padding: 16px 24px; background: var(--gray-bg); border-radius: 0 8px 8px 0; margin: 28px 0; font-size: 17px; font-style: italic; color: var(--heading); line-height: 1.65; }}
    .art-citations {{ font-size: 13px; color: #888; margin-top: 48px; padding-top: 20px; border-top: 1px solid #e8ecef; line-height: 1.7; }}
    .art-citations a {{ color: var(--teal); text-decoration: underline; }}

    /* BACK LINK */
    .art-back {{ margin-bottom: 36px; }}
    .art-back a {{ display: inline-flex; align-items: center; gap: 6px; font-size: 14px; font-weight: 600; color: var(--teal); font-family: 'Inter', sans-serif; transition: gap .18s; }}
    .art-back a:hover {{ gap: 10px; }}

    /* CTA */
    .art-cta {{ background: var(--teal); padding: 60px 20px; text-align: center; }}
    .art-cta-inner {{ max-width: 600px; margin: 0 auto; }}
    .art-cta h2 {{ font-size: 30px; font-weight: 700; color: #fff; margin-bottom: 12px; }}
    .art-cta p {{ font-size: 16px; color: rgba(255,255,255,.85); margin-bottom: 28px; }}
    .art-cta-btns {{ display: flex; gap: 16px; justify-content: center; flex-wrap: wrap; }}
    .btn-white {{ background: #fff; color: var(--teal); font-weight: 700; padding: 13px 28px; border-radius: 5px; font-size: 15px; font-family: 'Inter', sans-serif; display: inline-block; transition: opacity .18s; }}
    .btn-white:hover {{ opacity: .9; text-decoration: none; }}
    .btn-white-outline {{ background: transparent; color: #fff; border: 2px solid #fff; font-weight: 700; padding: 11px 26px; border-radius: 5px; font-size: 15px; font-family: 'Inter', sans-serif; display: inline-block; transition: opacity .18s; }}
    .btn-white-outline:hover {{ opacity: .8; text-decoration: none; }}

    /* TAGLINE + FOOTER */
    .tagline-strip {{ padding: 28px 20px; text-align: center; font-size: 15px; color: #666; }}
    footer {{ background: var(--heading); padding: 24px 20px; }}
    .footer-inner {{ max-width: var(--max-w); margin: 0 auto; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px; }}
    .footer-nav {{ display: flex; gap: 20px; list-style: none; }}
    .footer-nav a {{ font-size: 13px; color: rgba(255,255,255,.7); transition: color .18s; }}
    .footer-nav a:hover {{ color: var(--teal); }}
    .footer-right {{ display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }}
    .footer-right a {{ font-size: 13px; color: rgba(255,255,255,.7); display: flex; align-items: center; gap: 6px; transition: color .18s; }}
    .footer-right a:hover {{ color: var(--teal); }}
    .footer-copy {{ font-size: 12px; color: rgba(255,255,255,.4); }}

    @media (max-width: 900px) {{
      .nav-links {{ display: none; }}
      .nav-links.open {{ display: flex; flex-direction: column; align-items: stretch; position: fixed; top: 72px; left: 0; right: 0; background: #fff; box-shadow: 0 4px 16px rgba(0,0,0,.12); padding: 8px 0 16px; gap: 0; z-index: 199; }}
      .nav-links.open > li {{ width: 100%; }}
      .nav-links.open > li > a {{ display: block; padding: 11px 20px; font-size: 15px; border-bottom: 1px solid #f0f0f0; }}
      .nav-links.open .drop {{ display: block; position: static; border: none; box-shadow: none; border-radius: 0; min-width: unset; background: #f8f8f8; padding: 0; }}
      .nav-links.open .drop a {{ padding: 10px 20px 10px 32px; border-bottom: 1px solid #f0f0f0; }}
      .nav-links.open .nav-cta {{ display: block; margin: 12px 20px 0; text-align: center; border-radius: 5px; }}
      .nav-burger {{ display: block; }}
      .art-hero h1 {{ font-size: 28px; }}
      .art-body-inner h2 {{ font-size: 22px; }}
      .footer-inner {{ flex-direction: column; align-items: flex-start; }}
    }}
  </style>
</head>
<body>

{nav_html}

<section class="art-hero">
  <div class="art-hero-inner">
    <span class="art-tag">{label}</span>
    <h1>{title_esc}</h1>
    <p class="art-meta">Published {pub_date} &middot; {mins} min read</p>
  </div>
</section>

<section class="art-body">
  <div class="art-body-inner">
    <div class="art-back">
      <a href="/articles.html">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
        Back to Articles
      </a>
    </div>

{"" if not hero_img_path else f'<img src="{hero_img_path}" alt="{title_attr}" class="hero-img" />'}
{"" if not credit_html else credit_html}

{body_html}

{citations_html}
  </div>
</section>

<section class="art-cta">
  <div class="art-cta-inner">
    <h2>{brand_cfg.cta_headline}</h2>
    <p>{brand_cfg.cta_body}</p>
    <div class="art-cta-btns">
      <a href="{brand_cfg.cta_booking_url}" target="_blank" class="btn-white">{brand_cfg.cta_btn_primary}</a>
      <a href="{brand_cfg.cta_contact_url}" class="btn-white-outline">{brand_cfg.cta_btn_secondary}</a>
    </div>
  </div>
</section>

{footer_html}
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# HTML generation — article card
# ─────────────────────────────────────────────────────────────────────────────

def build_article_card(
    title: str,
    slug: str,
    article_type: str,
    brand_cfg: BrandConfig,
    hero_img_path: str | None = None,
) -> str:
    label = brand_cfg.article_type_label(article_type)
    gradient = brand_cfg.hero_gradient(article_type)
    title_esc = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if hero_img_path:
        card_img_src = hero_img_path.lstrip("/")
        img_html = (
            f'<div class="art-card-img" style="background: {gradient};">\n'
            f'        <img src="{card_img_src}" alt="{title_esc}" />\n'
            f'        <span class="art-badge">{label}</span>\n'
            f'      </div>'
        )
    else:
        img_html = (
            f'<div class="art-card-img" style="background: {gradient};">\n'
            f'        <span class="art-badge">{label}</span>\n'
            f'      </div>'
        )

    return f"""
    <a href="articles/{slug}.html" class="art-card">
      {img_html}
      <div class="art-card-body">
        <h2>{title_esc}</h2>
        <span class="art-read-more">Read More &raquo;</span>
      </div>
    </a>"""


# ─────────────────────────────────────────────────────────────────────────────
# articles.html update
# ─────────────────────────────────────────────────────────────────────────────

def insert_card_into_articles_index(
    site_path: Path,
    card_html: str,
    slug: str,
    brand_cfg: BrandConfig,
) -> None:
    articles_html_path = site_path / brand_cfg.articles_index
    content = articles_html_path.read_text(encoding="utf-8")

    if f'articles/{slug}.html' in content:
        raise ValueError(f"Slug '{slug}' already exists in articles.html — duplicate publish?")

    marker = brand_cfg.articles_grid_marker
    idx = content.find(marker)
    if idx == -1:
        raise ValueError(f"Could not find grid marker '{marker}' in articles.html")

    insert_after = idx + len(marker)
    updated = content[:insert_after] + "\n" + card_html + "\n" + content[insert_after:]
    articles_html_path.write_text(updated, encoding="utf-8")
    print("  ✓ Card prepended to articles.html")


# ─────────────────────────────────────────────────────────────────────────────
# Social variants
# ─────────────────────────────────────────────────────────────────────────────

def generate_social_variants(
    title: str,
    body: str,
    slug: str,
    brief: dict | None,
    pub_date: str,
    brand_cfg: BrandConfig,
) -> None:
    """Generate LinkedIn post + newsletter excerpt as text files in staging/social/."""
    brand_cfg.social_dir.mkdir(parents=True, exist_ok=True)

    article_url = f"{brand_cfg.article_url_base}/{slug}.html"

    plain = re.sub(r"#+\s+", "", body)
    plain = re.sub(r"\*\*([^*]+)\*\*", r"\1", plain)
    plain = re.sub(r"\*([^*]+)\*", r"\1", plain)
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", plain)
    plain = re.sub(r"---.*", "", plain, flags=re.DOTALL).strip()

    paragraphs = [p.strip() for p in re.split(r"\n\n+", plain) if p.strip() and not p.startswith("-")]

    first_para = paragraphs[0] if paragraphs else title
    second_para = paragraphs[1] if len(paragraphs) > 1 else ""

    social = brand_cfg.social
    article_type = brief.get("article_type", "") if brief else ""

    hashtags = social["linkedin_hashtags_base"]
    if article_type == "news-reaction":
        hashtags += " " + social.get("linkedin_hashtags_news", "")
    elif article_type == "how-to":
        hashtags += " " + social.get("linkedin_hashtags_how_to", "")
    elif article_type == "sector-analysis":
        hashtags += " " + social.get("linkedin_hashtags_sector", "")

    linkedin = f"""{first_para}

{second_para + chr(10) if second_para else ""}
Read the full article ↓
{article_url}

{hashtags.strip()}"""

    linkedin_path = brand_cfg.social_dir / f"linkedin-{slug}.txt"
    linkedin_path.write_text(linkedin.strip(), encoding="utf-8")
    print(f"  ✓ LinkedIn variant → {linkedin_path}")

    newsletter = f"""**{title}**

{first_para[:300].rstrip()}{"…" if len(first_para) > 300 else ""}

Read the full article: {article_url}"""

    newsletter_path = brand_cfg.social_dir / f"newsletter-{slug}.txt"
    newsletter_path.write_text(newsletter.strip(), encoding="utf-8")
    print(f"  ✓ Newsletter excerpt → {newsletter_path}")

    # X/Twitter — 280-char punchy version
    x_hashtags = social.get("x_hashtags", "")
    hook = first_para[:200].rstrip()
    if len(first_para) > 200:
        hook += "…"
    x_post = f"{hook}\n\n{article_url}\n\n{x_hashtags}".strip()
    x_path = brand_cfg.social_dir / f"x-{slug}.txt"
    x_path.write_text(x_post, encoding="utf-8")
    print(f"  ✓ X/Twitter variant → {x_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Git operations
# ─────────────────────────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: Path, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True, env=env)


def _get_gh_token(brand_cfg: BrandConfig) -> str:
    """
    Resolve a GitHub token for site pushes.

    Priority:
      1. Brand's token env var (e.g. CASHBUCKET_GH_TOKEN)
      2. gh auth token via GH_CONFIG_DIR=~/.config/gh-{brand} (local dev)
      3. Empty string — rely on existing remote URL / credential helper
    """
    token = os.environ.get(brand_cfg.gh_token_env, "")
    if token:
        return token
    gh_config = Path.home() / ".config" / f"gh-{brand_cfg.brand}"
    if gh_config.exists():
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                env={**os.environ, "GH_CONFIG_DIR": str(gh_config)},
                capture_output=True, text=True, check=True,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return ""


def git_push_site(site_path: Path, slug: str, brand_cfg: BrandConfig, dry_run: bool = False) -> None:
    """Commit article to a publish branch and open a PR with auto-merge."""
    token = _get_gh_token(brand_cfg)
    if token:
        remote_url = f"https://{brand_cfg.brand}-bot:{token}@github.com/{brand_cfg.site_repo}.git"
        _run(["git", "remote", "set-url", "origin", remote_url], cwd=site_path)

    files_to_stage = [
        f"{brand_cfg.articles_path}/{slug}.html",
        brand_cfg.articles_index,
    ]
    hero_img = site_path / brand_cfg.assets_path / f"art-{slug}.jpg"
    if hero_img.exists():
        files_to_stage.append(f"{brand_cfg.assets_path}/art-{slug}.jpg")
    _run(["git", "add"] + files_to_stage, cwd=site_path)

    status = _run(["git", "diff", "--cached", "--name-only"], cwd=site_path)
    if not status.stdout.strip():
        print("  ⚠  Nothing staged — article may already be published.")
        return

    _run(["git", "config", "user.name", f"{brand_cfg.brand}-bot"], cwd=site_path)
    _run(["git", "config", "user.email", f"bot@{brand_cfg.site_url.lstrip('https://')}"], cwd=site_path)

    branch = f"publish/{slug}"
    _run(["git", "checkout", "-b", branch], cwd=site_path)

    commit_msg = f"publish: {slug}\n\nPublished via content-engine pipeline/publisher.py"
    _run(["git", "commit", "-m", commit_msg], cwd=site_path)

    if dry_run:
        print("  ℹ  Dry run — commit created locally but NOT pushed.")
        _run(["git", "reset", "--soft", "HEAD~1"], cwd=site_path)
        return

    _run(["git", "push", "-u", "origin", branch], cwd=site_path)

    gh_env = {**os.environ}
    if token:
        gh_env["GH_TOKEN"] = token

    pr_create = _run(
        ["gh", "pr", "create",
         "--repo", brand_cfg.site_repo,
         "--title", f"publish: {slug}",
         "--body", "Published via content-engine pipeline/publisher.py",
         "--base", "main",
         "--head", branch],
        cwd=site_path,
        env=gh_env,
        check=False,
    )
    if pr_create.returncode != 0:
        if "already exists" in (pr_create.stdout + pr_create.stderr):
            print("  ℹ  PR already exists — skipping creation.")
        else:
            raise subprocess.CalledProcessError(
                pr_create.returncode, pr_create.args, pr_create.stdout, pr_create.stderr
            )

    merge = _run(
        ["gh", "pr", "merge", "--auto", "--squash",
         "--repo", brand_cfg.site_repo, branch],
        cwd=site_path,
        env=gh_env,
        check=False,
    )
    if merge.returncode == 0:
        print("  ✓ PR created and auto-merge enabled — article will deploy once checks pass.")
    else:
        print("  ✓ PR created — auto-merge not available on staging, merge manually.")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    repo_root = Path(__file__).parent.parent

    parser = argparse.ArgumentParser(
        description="Publish an approved draft to a brand site.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "draft_path",
        help="Path to the approved draft markdown. If relative, resolved against brand workspace.",
    )
    parser.add_argument(
        "--brand",
        default=DEFAULT_BRAND,
        help=f"Brand workspace to publish for (default: {DEFAULT_BRAND})",
    )
    parser.add_argument("--slug", default="", help="URL slug override")
    parser.add_argument("--description", default="", help="Meta description override (~155 chars)")
    parser.add_argument(
        "--publish-date",
        default=str(date.today()),
        help="Publication date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--site-path",
        default="",
        help="Path to site repo (default: ../{brand.site_local_name} relative to content-engine)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate all files but do not commit or push",
    )
    args = parser.parse_args()

    # ── Load brand config ────────────────────────────────────────────────────
    brand_cfg = load_brand(args.brand)
    nav_html = load_nav_html(brand_cfg.brand_dir)
    footer_html = load_footer_html(brand_cfg.brand_dir)

    # ── Resolve draft path ────────────────────────────────────────────────────
    draft_path = Path(args.draft_path)
    if not draft_path.is_absolute():
        # Try brand workspace first, then repo root
        brand_relative = brand_cfg.brand_dir / draft_path
        if brand_relative.exists():
            draft_path = brand_relative
        elif (repo_root / draft_path).exists():
            draft_path = repo_root / draft_path
    if not draft_path.exists():
        print(f"ERROR: Draft not found: {args.draft_path}", file=sys.stderr)
        return 1

    # ── Resolve site path ────────────────────────────────────────────────────
    if args.site_path:
        site_path = Path(args.site_path)
    else:
        site_path = repo_root.parent / brand_cfg.site_local_name
    if not site_path.exists():
        print(f"ERROR: Site repo not found at: {site_path}", file=sys.stderr)
        print(f"  Checkout {brand_cfg.site_repo} alongside content-engine, or pass --site-path.", file=sys.stderr)
        return 1

    # ── 1. Parse draft ───────────────────────────────────────────────────────
    print(f"\n[1/7] Parsing draft: {draft_path.name}  (brand: {brand_cfg.display_name})")
    title, body = parse_draft(draft_path)
    if not title:
        print("ERROR: Could not extract title from draft.", file=sys.stderr)
        return 1
    print(f"  Title: {title}")

    # ── 2. Load brief ─────────────────────────────────────────────────────────
    print("[2/7] Loading brief…")
    brief = load_brief(draft_path, brand_cfg)
    article_type = "explainer"
    if brief:
        article_type = brief.get("article_type", article_type)
        print(f"  Article type: {article_type}")
    else:
        print("  ⚠  No matching brief found — using default article_type='explainer'")

    # ── 2b. Compliance gate ───────────────────────────────────────────────────
    print("[2b/7] Running compliance gate…")
    full_text = f"# {title}\n\n{body}"
    gate = compliance_check(full_text, title=title, brief=brief, brand_name=brand_cfg.display_name)
    for w in gate.warnings:
        print(f"  ⚠  {w}")
    if not gate.passed:
        print("  ✗ Compliance gate FAILED — publication blocked.", file=sys.stderr)
        for f in gate.flags:
            print(f"    • {f}", file=sys.stderr)
        return 1
    print("  ✓ Compliance gate passed")

    # ── 3. Derive slug + description ──────────────────────────────────────────
    print("[3/7] Generating slug and description…")
    slug = args.slug or make_slug(title)
    description = args.description or make_description(body, brief)
    mins = read_time(body)
    print(f"  Slug: {slug}")
    print(f"  Description: {description[:80]}…")
    print(f"  Read time: {mins} min")

    # ── 4. Fetch hero image ───────────────────────────────────────────────────
    print("[4/7] Fetching hero image from Unsplash…")
    unsplash_key = os.environ.get(brand_cfg.unsplash_key_env, "")
    if not unsplash_key:
        print(f"  ℹ  {brand_cfg.unsplash_key_env} not set — skipping hero image.")
    hero_img_path, credit_html = fetch_unsplash_image(
        title=title,
        article_type=article_type,
        slug=slug,
        site_path=site_path,
        access_key=unsplash_key,
        brand_cfg=brand_cfg,
    )

    # ── 5. Convert markdown → HTML ────────────────────────────────────────────
    print("[5/7] Converting markdown to HTML…")
    body_html, citations_html = md_to_html(body)
    print(f"  HTML body: {len(body_html)} chars, citations: {'yes' if citations_html else 'none'}")

    # ── 6. Write article page ─────────────────────────────────────────────────
    print("[6/7] Writing article page…")
    article_html = build_article_html(
        title=title,
        slug=slug,
        body_html=body_html,
        citations_html=citations_html,
        article_type=article_type,
        description=description,
        pub_date=args.publish_date,
        mins=mins,
        brand_cfg=brand_cfg,
        nav_html=nav_html,
        footer_html=footer_html,
        hero_img_path=hero_img_path,
        credit_html=credit_html,
    )
    article_out = site_path / brand_cfg.articles_path / f"{slug}.html"
    if article_out.exists() and not args.dry_run:
        print(f"  ⚠  Article already exists: {article_out} — aborting.", file=sys.stderr)
        return 1
    article_out.write_text(article_html, encoding="utf-8")
    print(f"  ✓ Written: {article_out}")

    # ── 7. Update articles.html ───────────────────────────────────────────────
    print("[7/7] Updating articles.html…")
    card_html = build_article_card(title, slug, article_type, brand_cfg, hero_img_path)
    try:
        insert_card_into_articles_index(site_path, card_html, slug, brand_cfg)
    except ValueError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        article_out.unlink(missing_ok=True)
        return 1

    print("  Generating social variants…")
    generate_social_variants(title, body, slug, brief, args.publish_date, brand_cfg)

    # ── 8. Commit + push ──────────────────────────────────────────────────────
    print("[8/7] Committing and pushing to site repo…")
    try:
        git_push_site(site_path, slug, brand_cfg, dry_run=args.dry_run)
    except subprocess.CalledProcessError as e:
        print(f"  ERROR during git operation:\n  {e.stderr}", file=sys.stderr)
        return 1

    mode = "(DRY RUN — not pushed)" if args.dry_run else ""
    print(f"\n✅  Published {mode}")
    print(f"    Article: {brand_cfg.article_url_base}/{slug}.html")
    print(f"    Social variants: {brand_cfg.social_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
