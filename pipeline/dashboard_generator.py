"""
pipeline/dashboard_generator.py — Generate a brand's marketing dashboard HTML.

Brand-agnostic metrics dashboard for the marketing engine. All brand specifics
(title, theme colours, output path, repo slugs, token-expiry list, infra
workflows) come from the brand's `brand.yaml` — nothing here is hardcoded to a
single brand. A brand opts in by adding a `dashboard:` block to its brand.yaml;
brands without one simply don't render a dashboard.

Data sources (each degrades gracefully if its secret/config is absent):
  - Phase 1: local pipeline state (publish_queue.json, staging dirs) + token expiry
  - Phase 2: MailerLite subscriber/campaign stats + Buffer post stats
  - Phase 3: GA4 + Search Console (needs GOOGLE_OAUTH_JSON)
  - Phase 4: cal.com bookings + GitHub workflow health
  - Phase 5: SEO/GEO health checks (HTTP only)

Run:
  python3 -m pipeline.dashboard_generator --brand <slug> [--output path/to/index.html]

Output defaults to the brand's `dashboard.output_dir` (relative to the marketing
repo root), falling back to workers/{brand}-dashboard/public/index.html.
"""

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

from pipeline.brand_loader import consumer_root, load_brand

MAILERLITE_API_BASE = "https://connect.mailerlite.com/api"
BUFFER_GRAPHQL_URL = "https://api.buffer.com/graphql"
GA4_API_BASE = "https://analyticsdata.googleapis.com/v1beta"
GSC_API_BASE = "https://www.googleapis.com/webmasters/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL_API_BASE = "https://api.cal.com/v2"
CAL_API_VERSION = "2024-08-13"
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
HTTP_TIMEOUT = 12


# ---------------------------------------------------------------------------
# Data collection — Phase 1 (local files)
# ---------------------------------------------------------------------------

def _load_json(path: Path):
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return None


def _queue_stats(queue: list) -> dict:
    return {
        "published": [q for q in queue if q.get("status") == "published"],
        "approved": [q for q in queue if q.get("status") == "approved"],
        "queued": [q for q in queue if q.get("status") == "queued"],
        "held": [q for q in queue if q.get("status") == "hold"],
    }


def _social_stats(social_dir: Path) -> dict:
    if not social_dir.exists():
        return {"linkedin": 0, "x": 0}
    return {
        "linkedin": len(list(social_dir.glob("linkedin-*.txt"))),
        "x": len(list(social_dir.glob("x-*.txt"))),
    }


def _draft_count(drafts_dir: Path) -> int:
    if not drafts_dir.exists():
        return 0
    return len(list(drafts_dir.glob("draft-*-v*.md")))


def _parse_token_expiry(raw: dict) -> dict:
    """Normalise a brand's `dashboard.token_expiry` map to {label: date}.

    YAML parses an unquoted `2026-08-30` as a datetime.date already, but quoted
    values arrive as strings — accept both.
    """
    out: dict = {}
    for name, value in (raw or {}).items():
        if isinstance(value, date):
            out[name] = value
        else:
            try:
                out[name] = date.fromisoformat(str(value))
            except ValueError:
                continue
    return out


def _token_warnings(today: date, token_expiry: dict) -> list:
    result = []
    for name, exp_date in token_expiry.items():
        days = (exp_date - today).days
        status = "danger" if days <= 14 else "warning" if days <= 45 else "ok"
        result.append({"name": name, "expires": exp_date.isoformat(), "days": days, "status": status})
    return sorted(result, key=lambda x: x["days"])


def _buffer_pending(buffer_sent: dict) -> list:
    return [
        k.replace("linkedin:", "")
        for k, v in buffer_sent.items()
        if k.startswith("linkedin:") and not v.get("scheduled_at")
    ]


# ---------------------------------------------------------------------------
# Data collection — Phase 2 (MailerLite)
# ---------------------------------------------------------------------------

def _ml_get(api_key: str, path: str) -> tuple[bool, dict, str | None]:
    """GET from MailerLite connect API. Returns (ok, data, error)."""
    url = f"{MAILERLITE_API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return True, json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:
            detail = ""
        return False, {}, f"HTTP {e.code}: {detail}"
    except urllib.error.URLError as e:
        return False, {}, f"Network: {e.reason}"


def _fetch_mailerlite_stats(api_key: str | None) -> dict:
    """Fetch MailerLite subscriber count, last sent campaign, and automation status."""
    result: dict = {
        "subscriber_count": None,
        "last_campaign": None,
        "automations_active": None,
        "automations_total": None,
        "error": None,
    }
    if not api_key:
        result["error"] = "MAILERLITE_API_KEY not set"
        return result

    # Subscriber count (active only)
    ok, data, err = _ml_get(api_key, "/subscribers?filter[status]=active&page=1&limit=1")
    if ok:
        result["subscriber_count"] = data.get("meta", {}).get("total")
    else:
        result["error"] = err

    # Last sent campaign
    ok, data, err = _ml_get(api_key, "/campaigns?filter[status]=sent&sort=-sent_at&limit=1")
    if ok:
        campaigns = data.get("data", [])
        if campaigns:
            c = campaigns[0]
            stats = c.get("stats") or {}
            emails = c.get("emails") or [{}]
            sent_at_raw = c.get("sent_at", "")
            # Normalise sent_at to just the date portion
            sent_at = sent_at_raw[:10] if sent_at_raw else ""
            result["last_campaign"] = {
                "name": c.get("name", ""),
                "subject": (emails[0] or {}).get("subject", ""),
                "sent_at": sent_at,
                "open_rate": stats.get("open_rate"),
                "click_rate": stats.get("click_rate"),
                "unsubscribes": stats.get("unsubscribed"),
                "sent_count": stats.get("sent"),
            }

    # Automations (welcome sequence check)
    ok, data, err = _ml_get(api_key, "/automations")
    if ok:
        automations = data.get("data") or []
        result["automations_total"] = len(automations)
        result["automations_active"] = sum(1 for a in automations if a.get("enabled"))

    return result


# ---------------------------------------------------------------------------
# Data collection — Phase 2 (Buffer)
# ---------------------------------------------------------------------------

def _buffer_gql(access_token: str, query: str, variables: dict | None = None) -> tuple[bool, dict, str | None]:
    """Execute a Buffer GraphQL query. Returns (ok, data, error)."""
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        BUFFER_GRAPHQL_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        errors = body.get("errors")
        if errors:
            msgs = "; ".join(e.get("message", str(e)) for e in errors)
            return False, body.get("data") or {}, msgs
        return True, body.get("data") or {}, None
    except urllib.error.HTTPError as e:
        return False, {}, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, {}, f"Network: {e.reason}"


_BUFFER_SENT_POSTS_QUERY = """
query GetChannelSentPosts($channelId: String!) {
  channel(id: $channelId) {
    sentPosts(first: 50) {
      edges {
        node {
          id
          dueAt
          statistics {
            impressions
            clicks
          }
        }
      }
    }
  }
}
"""


def _fetch_buffer_channel_analytics(access_token: str, channel_id: str, cutoff: str) -> dict | None:
    """Try to fetch sent post analytics for one channel. Returns None on failure."""
    ok, data, _ = _buffer_gql(access_token, _BUFFER_SENT_POSTS_QUERY, {"channelId": channel_id})
    if not ok or not data.get("channel"):
        return None

    edges = (data["channel"].get("sentPosts") or {}).get("edges") or []
    posts_30d = 0
    impressions = 0
    clicks = 0
    has_stats = False

    for edge in edges:
        node = edge.get("node") or {}
        due_at = (node.get("dueAt") or "")[:10]
        if due_at and due_at < cutoff:
            continue
        posts_30d += 1
        stats = node.get("statistics") or {}
        if stats.get("impressions") is not None:
            has_stats = True
            impressions += stats.get("impressions") or 0
            clicks += stats.get("clicks") or 0

    return {
        "posts_30d": posts_30d,
        "impressions": impressions if has_stats else None,
        "clicks": clicks if has_stats else None,
    }


def _fetch_buffer_stats(access_token: str | None, brand_cfg, buffer_sent_raw: dict) -> dict:
    """Fetch Buffer post counts and (if available) analytics for the last 30 days.

    Always derives post counts from local buffer_sent.json as a reliable baseline.
    Attempts to fetch impressions/clicks from the Buffer API if a token is set.
    """
    cutoff = (date.today() - timedelta(days=30)).isoformat()

    result: dict = {
        "linkedin_posts_30d": 0,
        "x_posts_30d": 0,
        "linkedin_impressions": None,
        "linkedin_clicks": None,
        "x_impressions": None,
        "x_clicks": None,
        "analytics_available": False,
        "error": None,
    }

    # Derive counts from local state (always reliable)
    for key, meta in buffer_sent_raw.items():
        pushed = meta.get("pushed_at", "")
        if pushed >= cutoff:
            if key.startswith("linkedin:"):
                result["linkedin_posts_30d"] += 1
            elif key.startswith("x:"):
                result["x_posts_30d"] += 1

    if not access_token:
        result["error"] = "BUFFER_ACCESS_TOKEN not set"
        return result

    buf_cfg = brand_cfg.buffer
    for platform, channel_key in (("linkedin", "linkedin_channel_id"), ("x", "x_channel_id")):
        channel_id = buf_cfg.get(channel_key, "").strip()
        if not channel_id:
            continue
        analytics = _fetch_buffer_channel_analytics(access_token, channel_id, cutoff)
        if analytics is None:
            continue
        result["analytics_available"] = True
        if platform == "linkedin":
            result["linkedin_posts_30d"] = analytics["posts_30d"]
            result["linkedin_impressions"] = analytics["impressions"]
            result["linkedin_clicks"] = analytics["clicks"]
        else:
            result["x_posts_30d"] = analytics["posts_30d"]
            result["x_impressions"] = analytics["impressions"]
            result["x_clicks"] = analytics["clicks"]

    return result


def _quality_gate_status(queue_raw: list) -> dict | None:
    """Return quality gate info based on first publish date (gate opens at +28 days)."""
    published = [
        q for q in queue_raw
        if q.get("status") == "published" and q.get("published_at")
    ]
    if not published:
        return None
    first_pub = min(q["published_at"] for q in published)
    try:
        first_date = date.fromisoformat(first_pub)
    except ValueError:
        return None
    gate_date = first_date + timedelta(days=28)
    today = date.today()
    days_remaining = (gate_date - today).days
    return {
        "first_published": first_pub,
        "gate_date": gate_date.isoformat(),
        "days_remaining": days_remaining,
        "open": days_remaining <= 0,
    }


# ---------------------------------------------------------------------------
# Data collection — Phase 3 (GA4 + Search Console)
# ---------------------------------------------------------------------------

def _google_access_token(oauth_json: str | None) -> tuple[str | None, str | None]:
    """Exchange an OAuth refresh token for a short-lived access token. Returns (token, error).

    oauth_json is a JSON string with keys: client_id, client_secret, refresh_token.
    Generate it once with scripts/google_oauth_setup.py, then store as GOOGLE_OAUTH_JSON secret.
    """
    if not oauth_json:
        return None, "GOOGLE_OAUTH_JSON not set"
    try:
        info = json.loads(oauth_json)
    except Exception:
        return None, "GOOGLE_OAUTH_JSON is not valid JSON"
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not info.get(k)]
    if missing:
        return None, f"GOOGLE_OAUTH_JSON missing keys: {', '.join(missing)}"
    data = urllib.parse.urlencode({
        "client_id": info["client_id"],
        "client_secret": info["client_secret"],
        "refresh_token": info["refresh_token"],
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(
        GOOGLE_TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            token = result.get("access_token")
            if not token:
                return None, f"No access_token in response: {result}"
            return token, None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
            msg = detail.get("error_description") or detail.get("error") or str(e.code)
        except Exception:
            msg = str(e.code)
        return None, f"Token refresh failed: {msg}"
    except urllib.error.URLError as e:
        return None, f"Network error refreshing token: {e.reason}"


def _google_post(token: str, url: str, payload: dict) -> tuple[bool, dict, str | None]:
    """POST to a Google REST API with a Bearer token. Returns (ok, data, error)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return True, json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            detail = ""
        return False, {}, f"HTTP {e.code}: {detail}"
    except urllib.error.URLError as e:
        return False, {}, f"Network: {e.reason}"


def _fetch_ga4_stats(token: str | None, property_id: str) -> dict:
    """Fetch GA4 sessions, top pages, and traffic sources for the last 7 and 28 days."""
    result: dict = {
        "sessions_7d": None,
        "sessions_28d": None,
        "sessions_28d_prior": None,
        "top_pages": [],
        "sources": [],
        "error": None,
    }
    if not token:
        result["error"] = "No Google access token"
        return result

    base = f"{GA4_API_BASE}/properties/{property_id}:runReport"

    # Sessions — 7d (metricAggregations ensures totals field is populated)
    ok, data, err = _google_post(token, base, {
        "dateRanges": [{"startDate": "7daysAgo", "endDate": "today"}],
        "metrics": [{"name": "sessions"}],
        "metricAggregations": ["TOTAL"],
    })
    if not ok:
        result["error"] = err
        return result
    totals = data.get("totals") or []
    if totals:
        result["sessions_7d"] = int((totals[0].get("metricValues") or [{}])[0].get("value", 0))

    # Sessions — 28d (current) and prior 28d in one request using GA4 multi-date-range
    ok, data, _ = _google_post(token, base, {
        "dateRanges": [
            {"startDate": "28daysAgo", "endDate": "today"},
            {"startDate": "56daysAgo", "endDate": "29daysAgo"},
        ],
        "metrics": [{"name": "sessions"}],
        "metricAggregations": ["TOTAL"],
    })
    if ok:
        totals = data.get("totals") or []
        if totals:
            result["sessions_28d"] = int((totals[0].get("metricValues") or [{}])[0].get("value", 0))
        if len(totals) > 1:
            result["sessions_28d_prior"] = int((totals[1].get("metricValues") or [{}])[0].get("value", 0))

    # Top 5 pages by sessions (28d)
    ok, data, _ = _google_post(token, base, {
        "dateRanges": [{"startDate": "28daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "sessions"}, {"name": "averageSessionDuration"}],
        "limit": 5,
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
    })
    if ok:
        for row in data.get("rows") or []:
            dims = row.get("dimensionValues") or []
            mets = row.get("metricValues") or []
            if dims and mets:
                result["top_pages"].append({
                    "path": dims[0].get("value", ""),
                    "sessions": int(mets[0].get("value", 0)),
                    "avg_duration_s": float(mets[1].get("value", 0)) if len(mets) > 1 else 0.0,
                })

    # Traffic sources (28d)
    ok, data, _ = _google_post(token, base, {
        "dateRanges": [{"startDate": "28daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "sessionDefaultChannelGroup"}],
        "metrics": [{"name": "sessions"}],
        "limit": 6,
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
    })
    if ok:
        for row in data.get("rows") or []:
            dims = row.get("dimensionValues") or []
            mets = row.get("metricValues") or []
            if dims and mets:
                result["sources"].append({
                    "channel": dims[0].get("value", ""),
                    "sessions": int(mets[0].get("value", 0)),
                })

    return result


def _fetch_gsc_stats(token: str | None, gsc_site_url: str) -> dict:
    """Fetch Search Console clicks, impressions, avg position, top queries, CTR opps (28d)."""
    result: dict = {
        "clicks_28d": None,
        "impressions_28d": None,
        "avg_position": None,
        "clicks_28d_prior": None,
        "impressions_28d_prior": None,
        "avg_position_prior": None,
        "top_queries": [],
        "ctr_opportunities": [],
        "near_page_1": [],
        "zero_click": [],
        "error": None,
    }
    if not token:
        result["error"] = "No Google access token"
        return result

    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=28)).isoformat()
    encoded_site = urllib.parse.quote(gsc_site_url, safe="")
    url = f"{GSC_API_BASE}/sites/{encoded_site}/searchAnalytics/query"

    # Totals via date dimension (sum over days)
    ok, data, err = _google_post(token, url, {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["date"],
        "rowLimit": 500,
    })
    if not ok:
        result["error"] = err
        return result
    rows = data.get("rows") or []
    if rows:
        total_clicks = sum(int(r.get("clicks", 0)) for r in rows)
        total_impressions = sum(int(r.get("impressions", 0)) for r in rows)
        result["clicks_28d"] = total_clicks
        result["impressions_28d"] = total_impressions
        if total_impressions:
            weighted_pos = sum(float(r.get("position", 0)) * int(r.get("impressions", 0)) for r in rows)
            result["avg_position"] = round(weighted_pos / total_impressions, 1)

    # All queries — fetch enough rows to derive ranking gap analysis
    ok, data, _ = _google_post(token, url, {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["query"],
        "rowLimit": 5000,
    })
    if ok:
        all_queries = []
        for row in data.get("rows") or []:
            all_queries.append({
                "query": (row.get("keys") or [""])[0],
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "ctr": round(float(row.get("ctr", 0)) * 100, 1),
                "position": round(float(row.get("position", 0)), 1),
            })
        # Top queries by clicks (existing)
        result["top_queries"] = sorted(all_queries, key=lambda x: -x["clicks"])[:10]
        # CTR opportunities: >50 impressions, <2% CTR
        result["ctr_opportunities"] = sorted(
            [q for q in all_queries if q["impressions"] >= 50 and q["ctr"] < 2.0],
            key=lambda x: -x["impressions"],
        )[:5]
        # Near-page-1: positions 11–20, at least 2 impressions
        result["near_page_1"] = sorted(
            [q for q in all_queries if 11 <= q["position"] <= 20 and q["impressions"] >= 2],
            key=lambda x: (-x["impressions"], x["position"]),
        )[:10]
        # Zero-click: impressions >= 3, no clicks, position <= 30
        result["zero_click"] = sorted(
            [q for q in all_queries if q["clicks"] == 0 and q["impressions"] >= 3 and q["position"] <= 30],
            key=lambda x: (-x["impressions"], x["position"]),
        )[:10]

    # Prior 28d totals for trend comparison
    prior_start = (date.today() - timedelta(days=56)).isoformat()
    prior_end = (date.today() - timedelta(days=29)).isoformat()
    ok, data, _ = _google_post(token, url, {
        "startDate": prior_start,
        "endDate": prior_end,
        "dimensions": ["date"],
        "rowLimit": 500,
    })
    if ok:
        prior_rows = data.get("rows") or []
        if prior_rows:
            prior_clicks = sum(int(r.get("clicks", 0)) for r in prior_rows)
            prior_impressions = sum(int(r.get("impressions", 0)) for r in prior_rows)
            result["clicks_28d_prior"] = prior_clicks
            result["impressions_28d_prior"] = prior_impressions
            if prior_impressions:
                weighted_pos = sum(float(r.get("position", 0)) * int(r.get("impressions", 0)) for r in prior_rows)
                result["avg_position_prior"] = round(weighted_pos / prior_impressions, 1)

    return result


# ---------------------------------------------------------------------------
# Data collection — Phase 4 (cal.com + GitHub infrastructure)
# ---------------------------------------------------------------------------

def _fetch_calcom_stats(api_key: str | None) -> dict:
    """Fetch cal.com bookings counts for last 7 and 30 days (accepted bookings only)."""
    result: dict = {
        "bookings_7d": None,
        "bookings_30d": None,
        "error": None,
    }
    if not api_key:
        result["error"] = "CAL_API_KEY not set"
        return result

    url = f"{CAL_API_BASE}/bookings"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "cal-api-version": CAL_API_VERSION,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        bookings = data.get("bookings") or []

        today = date.today()
        cutoff_7d = (today - timedelta(days=7)).isoformat()
        cutoff_30d = (today - timedelta(days=30)).isoformat()

        bookings_7d = 0
        bookings_30d = 0
        for booking in bookings:
            if booking.get("status") != "accepted":
                continue
            start = booking.get("start") or ""
            if start[:10] >= cutoff_7d:
                bookings_7d += 1
                bookings_30d += 1
            elif start[:10] >= cutoff_30d:
                bookings_30d += 1

        result["bookings_7d"] = bookings_7d
        result["bookings_30d"] = bookings_30d
        return result
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:
            detail = ""
        result["error"] = f"HTTP {e.code}: {detail}"
        return result
    except urllib.error.URLError as e:
        result["error"] = f"Network: {e.reason}"
        return result
    except Exception as e:
        result["error"] = f"Error: {str(e)}"
        return result


def _fetch_github_infra_stats(token: str | None, marketing_repo: str, workflows: list) -> dict:
    """Fetch last-run status for the brand's key GitHub workflows.

    `workflows` is a list of [workflow_file, display_name] pairs from brand.yaml.
    """
    result: dict = {
        "workflows": [],
        "error": None,
    }
    if not token:
        result["error"] = "GitHub token not set"
        return result
    if not marketing_repo:
        result["error"] = "dashboard.marketing_repo not set"
        return result

    for entry in workflows:
        try:
            workflow_name, display_name = entry[0], entry[1]
        except (IndexError, TypeError):
            continue
        url = (
            f"{GITHUB_API_BASE}/repos/{marketing_repo}"
            f"/actions/workflows/{workflow_name}/runs?per_page=1"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            runs = data.get("workflow_runs") or []
            if runs:
                run = runs[0]
                status = run.get("conclusion") or run.get("status") or "unknown"
                updated_at = run.get("updated_at") or ""
                run_at = updated_at[:10] if updated_at else ""
                result["workflows"].append({
                    "name": display_name,
                    "status": status,
                    "run_at": run_at,
                    "url": run.get("html_url", ""),
                })
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Data collection — Phase 5 (SEO/GEO health checks)
# ---------------------------------------------------------------------------

def _http_check(url: str) -> tuple[bool, int | None, str | None]:
    """HEAD-check a URL. Returns (ok, status_code, error)."""
    req = urllib.request.Request(url, method="HEAD")
    req.add_header("User-Agent", "MarketingDashboard/1.0")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return True, resp.status, None
    except urllib.error.HTTPError as e:
        return e.code == 200, e.code, None
    except urllib.error.URLError as e:
        return False, None, str(e.reason)


def _count_sitemap_urls(base_url: str) -> tuple[int | None, str | None]:
    """Fetch and parse sitemap.xml, returning (url_count, error)."""
    url = f"{base_url.rstrip('/')}/sitemap.xml"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "MarketingDashboard/1.0")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
        ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
        locs = root.findall(f".//{{{ns}}}loc")
        return len(locs), None
    except ET.ParseError as e:
        return None, f"Parse error: {e}"
    except urllib.error.URLError as e:
        return None, str(e.reason)
    except Exception as e:
        return None, str(e)


def _seo_health(base_url: str) -> dict:
    """Run HTTP checks for sitemap, llms.txt, and robots.txt."""
    base = base_url.rstrip("/")
    sitemap_count, sitemap_err = _count_sitemap_urls(base)
    llms_ok, _, llms_err = _http_check(f"{base}/llms.txt")
    robots_ok, _, robots_err = _http_check(f"{base}/robots.txt")
    return {
        "sitemap_count": sitemap_count,
        "sitemap_error": sitemap_err,
        "sitemap_url": f"{base}/sitemap.xml",
        "llms_txt_ok": llms_ok,
        "llms_txt_error": llms_err,
        "llms_txt_url": f"{base}/llms.txt",
        "robots_txt_ok": robots_ok,
        "robots_txt_error": robots_err,
        "robots_txt_url": f"{base}/robots.txt",
    }


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------

def _link(url: str, text: str) -> str:
    """Render text as an external link, or plain text when no URL is available."""
    if not url:
        return text
    return f'<a href="{url}" target="_blank" rel="noopener">{text}</a>'


def _trend_html(current, prior, invert: bool = False) -> str:
    """Return a trend span comparing current to prior period.

    invert=True for metrics where lower is better (avg position).
    """
    if current is None or prior is None or prior == 0:
        return ""
    pct = round((current - prior) / prior * 100)
    going_up = (pct > 0) if not invert else (pct < 0)
    if pct == 0:
        return '<span class="trend-flat">→ 0% vs prior 28d</span>'
    arrow = "↑" if going_up else "↓"
    css = "trend-up" if going_up else "trend-down"
    sign = "+" if going_up else "−"
    return f'<span class="{css}">{arrow} {sign}{abs(pct)}% vs prior 28d</span>'


def _badge(status: str, label: str) -> str:
    return f'<span class="badge badge-{status}">{label}</span>'


def _days_label(days: int) -> str:
    if days <= 0:
        return "EXPIRED"
    return f"{days} day{'s' if days != 1 else ''}"


def _slug_list(items: list, key: str = "slug", limit: int = 3) -> str:
    rows = ""
    for item in items[:limit]:
        slug = item.get(key, "—")
        rows += f"<li>{slug}</li>"
    return f'<ul class="slug-list">{rows}</ul>' if rows else "<p>—</p>"


def _approve_list(items: list, staging_site_url: str, marketing_repo: str, brand_slug: str,
                  key: str = "slug", limit: int = 3) -> str:
    rows = ""
    for item in items[:limit]:
        slug = item.get(key, "—")
        preview_url = f"{staging_site_url}/articles/{slug}"
        draft_path = item.get("draft_path", "")
        gh_url = (
            f"https://github.com/{marketing_repo}/blob/main/brands/{brand_slug}/{draft_path}"
            if (marketing_repo and draft_path) else ""
        )
        slug_html = _link(gh_url, slug)
        rows += (
            f'<li class="approve-item">'
            f'<span class="approve-slug">{slug_html}</span>'
            f'<button class="stage-btn" data-slug="{slug}" data-preview-url="{preview_url}">Preview</button>'
            f'<button class="approve-btn" data-slug="{slug}">Approve</button>'
            f'</li>'
        )
    return f'<ul class="slug-list approve-list" id="pipeline-approve-list">{rows}</ul>' if rows else "<p>—</p>"


def _card(label: str, value, detail: str = "", border_color: str = "var(--primary)", card_id: str = "") -> str:
    id_attr = f' id="{card_id}"' if card_id else ""
    return f"""
    <div class="card"{id_attr} style="border-top-color:{border_color}">
      <div class="card-label">{label}</div>
      <div class="card-value">{value}</div>
      <div class="card-detail">{detail}</div>
    </div>"""


def _token_card(token: dict) -> str:
    days_html = f'<span class="token-days badge badge-{token["status"]}">{_days_label(token["days"])}</span>'
    return f"""
    <div class="token-card">
      <div>
        <div class="token-name">{token["name"]}</div>
        <div class="token-date">Expires {token["expires"]}</div>
      </div>
      {days_html}
    </div>"""


def _coming_soon_card(title: str, phase: str) -> str:
    return f"""
    <div class="coming-soon">
      <div class="coming-soon-title">{title}</div>
      <div class="coming-soon-phase">{phase}</div>
    </div>"""


def _check_row(label: str, ok: bool | None, detail: str = "") -> str:
    if ok is None:
        badge_cls = "badge-warning"
        badge_text = "Unknown"
    elif ok:
        badge_cls = "badge-ok"
        badge_text = "OK"
    else:
        badge_cls = "badge-danger"
        badge_text = "Not found"
    return f"""
    <div class="check-row">
      <div class="check-label">{label}</div>
      <div class="check-detail">{detail}</div>
      <span class="badge {badge_cls}">{badge_text}</span>
    </div>"""


def _fmt_rate(rate) -> str:
    """Format a rate value (string or number) as a percentage string."""
    if rate is None:
        return "—"
    try:
        return f"{float(rate):.1f}%"
    except (TypeError, ValueError):
        return str(rate)


def _fmt_num(n) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}"


# ---------------------------------------------------------------------------
# Section HTML builders
# ---------------------------------------------------------------------------

def _section_newsletter_html(ml: dict) -> str:
    if ml.get("error") and ml.get("subscriber_count") is None:
        err_note = f'<p class="api-error">MailerLite: {ml["error"]}</p>'
        return f'<div class="section"><div class="section-title">Newsletter (MailerLite)</div>{err_note}</div>'

    sub_count = _fmt_num(ml.get("subscriber_count"))

    last = ml.get("last_campaign")
    if last:
        subject = last.get("subject") or last.get("name") or "—"
        sent_at = last.get("sent_at") or "—"
        open_rate = _fmt_rate(last.get("open_rate"))
        click_rate = _fmt_rate(last.get("click_rate"))
        campaign_detail = (
            f'<span title="{subject}" style="display:block;overflow:hidden;text-overflow:ellipsis;'
            f'white-space:nowrap;max-width:180px">{subject}</span>'
            f'Sent {sent_at}<br>Open {open_rate} &middot; Click {click_rate}'
        )
    else:
        campaign_detail = "No sent campaigns yet"

    auto_total = ml.get("automations_total")
    auto_active = ml.get("automations_active")
    if auto_total is None:
        auto_value = "—"
        auto_detail = "Could not fetch automations"
    elif auto_total == 0:
        auto_value = "None"
        auto_detail = "No automations configured"
    elif auto_active == 0:
        auto_value = "Inactive"
        auto_detail = f"{auto_total} automation(s) configured, none enabled"
    else:
        auto_value = f"{auto_active}/{auto_total}"
        auto_detail = "automation(s) enabled"

    auto_color = "var(--primary)" if (auto_active or 0) > 0 else "var(--accent)"

    cards = (
        _card("Subscribers", sub_count, "Active subscribers") +
        _card("Last Campaign", "Sent" if last else "—", campaign_detail) +
        _card("Automations", auto_value, auto_detail, border_color=auto_color)
    )

    error_note = f'<p class="api-error">{ml["error"]}</p>' if ml.get("error") else ""

    return f"""
  <div class="section">
    <div class="section-title">Newsletter (MailerLite)</div>
    {error_note}
    <div class="card-grid">
      {cards}
    </div>
  </div>"""


def _section_social_html(buf: dict, quality_gate: dict | None, buffer_pending: list) -> str:
    def _stat_detail(posts, impressions, clicks) -> str:
        parts = [f"{posts} post{'s' if posts != 1 else ''}"]
        if impressions is not None:
            parts.append(f"{_fmt_num(impressions)} impressions")
        if clicks is not None:
            parts.append(f"{_fmt_num(clicks)} clicks")
        return " &middot; ".join(parts) if len(parts) > 1 else parts[0]

    li_detail = _stat_detail(buf["linkedin_posts_30d"], buf["linkedin_impressions"], buf["linkedin_clicks"])
    x_detail = _stat_detail(buf["x_posts_30d"], buf["x_impressions"], buf["x_clicks"])

    # Only surface "plan upgrade" note when token is present but analytics weren't returned
    token_present = not buf.get("error") or "not set" not in (buf.get("error") or "")
    if token_present and not buf["analytics_available"]:
        analytics_note = '<p class="api-note">Impressions/clicks not available — Buffer Analyze plan required</p>'
    else:
        analytics_note = ""

    pending_count = len(buffer_pending)
    pending_detail = (
        (_slug_list([{"slug": s} for s in buffer_pending], limit=3) if buffer_pending else "None pending")
    )
    pending_color = "var(--accent)" if pending_count > 5 else "var(--primary)"

    if quality_gate is None:
        gate_value = "—"
        gate_detail = "No published articles yet"
        gate_color = "var(--primary)"
    elif quality_gate["open"]:
        gate_value = "Open"
        gate_detail = f"4-week gate passed on {quality_gate['gate_date']}"
        gate_color = "var(--primary)"
    else:
        days = quality_gate["days_remaining"]
        gate_value = f"{days}d"
        gate_detail = f"Gate opens {quality_gate['gate_date']}<br>First published {quality_gate['first_published']}"
        gate_color = "var(--accent)" if days <= 7 else "var(--primary)"

    cards = (
        _card("LinkedIn (30d)", buf["linkedin_posts_30d"], li_detail) +
        _card("X / Twitter (30d)", buf["x_posts_30d"], x_detail) +
        _card("Pending Approval", pending_count,
              f"LinkedIn drafts awaiting Kevin<br>{pending_detail}", border_color=pending_color) +
        _card("Quality Gate", gate_value, gate_detail, border_color=gate_color)
    )

    error_note = f'<p class="api-error">{buf["error"]}</p>' if buf.get("error") else ""

    return f"""
  <div class="section">
    <div class="section-title">Social (Buffer)</div>
    {error_note}
    {analytics_note}
    <div class="card-grid">
      {cards}
    </div>
  </div>"""


def _fmt_duration(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _section_ga4_html(ga4: dict) -> str:
    if ga4.get("error") and ga4.get("sessions_7d") is None:
        err_note = f'<p class="api-error">GA4: {ga4["error"]}</p>'
        return f'<div class="section"><div class="section-title">Web Traffic (GA4)</div>{err_note}</div>'

    s7 = _fmt_num(ga4.get("sessions_7d"))
    s28 = _fmt_num(ga4.get("sessions_28d"))
    s28_trend = _trend_html(ga4.get("sessions_28d"), ga4.get("sessions_28d_prior"))

    # Top pages table
    pages = ga4.get("top_pages") or []
    if pages:
        rows_html = "".join(
            f'<tr><td class="q-cell">{p["path"]}</td>'
            f'<td class="n-cell">{_fmt_num(p["sessions"])}</td>'
            f'<td class="n-cell">{_fmt_duration(p["avg_duration_s"])}</td></tr>'
            for p in pages
        )
        pages_table = f"""
        <table class="data-table">
          <thead><tr><th>Page</th><th>Sessions</th><th>Avg time</th></tr></thead>
          <tbody>{rows_html}</tbody>
        </table>"""
    else:
        pages_table = "<p style='color:#999;font-size:0.8rem'>No page data yet</p>"

    # Sources breakdown
    sources = ga4.get("sources") or []
    if sources:
        total_src = sum(s["sessions"] for s in sources) or 1
        src_items = "".join(
            f'<div class="src-row"><span class="src-name">{s["channel"]}</span>'
            + '<span class="src-bar-wrap"><span class="src-bar" style="width:'
            + f'{min(100, round(s["sessions"] / total_src * 100))}%">'
            + f'</span></span><span class="src-count">{_fmt_num(s["sessions"])}</span></div>'
            for s in sources
        )
        sources_html = f'<div class="src-list">{src_items}</div>'
    else:
        sources_html = "<p style='color:#999;font-size:0.8rem'>No source data yet</p>"

    error_note = f'<p class="api-error">{ga4["error"]}</p>' if ga4.get("error") else ""

    return f"""
  <div class="section">
    <div class="section-title">Web Traffic (GA4 · last 28d)</div>
    {error_note}
    <div class="card-grid" style="margin-bottom:20px">
      {_card("Sessions (7d)", s7, "Last 7 days")}
      {_card("Sessions (28d)", s28, "Last 28 days" + (f"<br>{s28_trend}" if s28_trend else ""))}
    </div>
    <div class="two-col">
      <div>
        <div class="subsection-label">Top pages (28d)</div>
        {pages_table}
      </div>
      <div>
        <div class="subsection-label">Traffic sources (28d)</div>
        {sources_html}
      </div>
    </div>
  </div>"""


def _section_gsc_html(gsc: dict) -> str:
    if gsc.get("error") and gsc.get("clicks_28d") is None:
        err_note = f'<p class="api-error">Search Console: {gsc["error"]}</p>'
        return f'<div class="section"><div class="section-title">Search Visibility (GSC)</div>{err_note}</div>'

    clicks = _fmt_num(gsc.get("clicks_28d"))
    impressions = _fmt_num(gsc.get("impressions_28d"))
    avg_pos = f'{gsc["avg_position"]:.1f}' if gsc.get("avg_position") is not None else "—"
    pos_color = "var(--primary)" if (gsc.get("avg_position") or 99) <= 20 else "var(--accent)"
    clicks_trend = _trend_html(gsc.get("clicks_28d"), gsc.get("clicks_28d_prior"))
    impressions_trend = _trend_html(gsc.get("impressions_28d"), gsc.get("impressions_28d_prior"))
    pos_trend = _trend_html(gsc.get("avg_position"), gsc.get("avg_position_prior"), invert=True)

    # Top queries table
    queries = gsc.get("top_queries") or []
    if queries:
        q_rows = "".join(
            f'<tr><td class="q-cell">{q["query"]}</td>'
            f'<td class="n-cell">{q["clicks"]}</td>'
            f'<td class="n-cell">{q["impressions"]}</td>'
            f'<td class="n-cell">{q["ctr"]}%</td>'
            f'<td class="n-cell">{q["position"]}</td></tr>'
            for q in queries
        )
        queries_table = f"""
        <table class="data-table">
          <thead><tr><th>Query</th><th>Clicks</th><th>Impr</th><th>CTR</th><th>Pos</th></tr></thead>
          <tbody>{q_rows}</tbody>
        </table>"""
    else:
        queries_table = "<p style='color:#999;font-size:0.8rem'>No query data yet</p>"

    # CTR opportunities
    opps = gsc.get("ctr_opportunities") or []
    if opps:
        opp_rows = "".join(
            f'<tr><td class="q-cell">{o["query"]}</td>'
            f'<td class="n-cell">{o["impressions"]}</td>'
            f'<td class="n-cell">{o["ctr"]}%</td>'
            f'<td class="n-cell">{o["position"]}</td></tr>'
            for o in opps
        )
        opps_html = f"""
        <table class="data-table">
          <thead><tr><th>Query</th><th>Impr</th><th>CTR</th><th>Pos</th></tr></thead>
          <tbody>{opp_rows}</tbody>
        </table>"""
    else:
        opps_html = "<p style='color:#999;font-size:0.8rem'>None yet (&gt;50 impressions, &lt;2% CTR)</p>"

    # Near-page-1 queries (positions 11–20)
    near_p1 = gsc.get("near_page_1") or []
    if near_p1:
        np1_rows = "".join(
            f'<tr><td class="q-cell">{q["query"]}</td>'
            f'<td class="n-cell">{q["impressions"]}</td>'
            f'<td class="n-cell">{q["clicks"]}</td>'
            f'<td class="n-cell">{q["position"]}</td></tr>'
            for q in near_p1
        )
        near_p1_html = f"""
        <table class="data-table">
          <thead><tr><th>Query</th><th>Impr</th><th>Clicks</th><th>Pos</th></tr></thead>
          <tbody>{np1_rows}</tbody>
        </table>"""
    else:
        near_p1_html = "<p style='color:#999;font-size:0.8rem'>None yet (pos 11–20, &ge;2 impressions)</p>"

    # Zero-click queries with impressions
    zero_click = gsc.get("zero_click") or []
    if zero_click:
        zc_rows = "".join(
            f'<tr><td class="q-cell">{q["query"]}</td>'
            f'<td class="n-cell">{q["impressions"]}</td>'
            f'<td class="n-cell">{q["position"]}</td></tr>'
            for q in zero_click
        )
        zero_click_html = f"""
        <table class="data-table">
          <thead><tr><th>Query</th><th>Impr</th><th>Pos</th></tr></thead>
          <tbody>{zc_rows}</tbody>
        </table>"""
    else:
        zero_click_html = (
            "<p style='color:#999;font-size:0.8rem'>"
            "None yet (&ge;3 impressions, 0 clicks, pos &le;30)"
            "</p>"
        )

    error_note = f'<p class="api-error">{gsc["error"]}</p>' if gsc.get("error") else ""
    clicks_note = f"<br>{clicks_trend}" if clicks_trend else ""
    impressions_note = f"<br>{impressions_trend}" if impressions_trend else ""
    pos_note = f"<br>{pos_trend}" if pos_trend else ""

    return f"""
  <div class="section">
    <div class="section-title">Search Visibility (Search Console · last 28d)</div>
    {error_note}
    <div class="card-grid" style="margin-bottom:20px">
      {_card("Clicks (28d)", clicks, "Organic search clicks" + clicks_note)}
      {_card("Impressions (28d)", impressions, "Search impressions" + impressions_note)}
      {_card("Avg Position", avg_pos, "Weighted average ranking" + pos_note, border_color=pos_color)}
    </div>
    <div class="two-col">
      <div>
        <div class="subsection-label">Top queries by clicks</div>
        {queries_table}
      </div>
      <div>
        <div class="subsection-label">CTR opportunities (&gt;50 impr, &lt;2% CTR)</div>
        {opps_html}
      </div>
    </div>
    <div class="two-col" style="margin-top:16px">
      <div>
        <div class="subsection-label">Near page 1 (pos 11–20)</div>
        {near_p1_html}
      </div>
      <div>
        <div class="subsection-label">0-click queries with impressions</div>
        {zero_click_html}
      </div>
    </div>
  </div>"""


def _section_seo_html(seo: dict) -> str:
    sitemap_count = seo["sitemap_count"]
    sitemap_ok = sitemap_count is not None
    sitemap_detail = f"{sitemap_count} URLs" if sitemap_ok else (seo["sitemap_error"] or "")
    sitemap_label = _link(seo.get("sitemap_url", ""), "sitemap.xml")
    sitemap_detail_full = f"{sitemap_label} &middot; {sitemap_detail}"

    llms_detail = (
        _link(seo.get("llms_txt_url", ""), seo.get("llms_txt_url", ""))
        if seo["llms_txt_ok"]
        else (seo.get("llms_txt_error") or "")
    )
    robots_detail = (
        _link(seo.get("robots_txt_url", ""), seo.get("robots_txt_url", ""))
        if seo["robots_txt_ok"]
        else (seo.get("robots_txt_error") or "")
    )
    rows = (
        _check_row("sitemap.xml", sitemap_ok, sitemap_detail_full)
        + _check_row("llms.txt", seo["llms_txt_ok"], llms_detail)
        + _check_row("robots.txt", seo["robots_txt_ok"], robots_detail)
    )

    return f"""
  <div class="section">
    <div class="section-title">SEO / GEO Health</div>
    <div class="check-list">
      {rows}
    </div>
  </div>"""


def _section_bookings_html(cal: dict) -> str:
    if cal.get("error") and cal.get("bookings_7d") is None:
        err_note = f'<p class="api-error">cal.com: {cal["error"]}</p>'
        return f'<div class="section"><div class="section-title">Bookings (cal.com)</div>{err_note}</div>'

    b7 = _fmt_num(cal.get("bookings_7d"))
    b30 = _fmt_num(cal.get("bookings_30d"))

    cards = (
        _card("Last 7 days", b7, "Accepted bookings") +
        _card("Last 30 days", b30, "Accepted bookings")
    )

    error_note = f'<p class="api-error">{cal["error"]}</p>' if cal.get("error") else ""

    return f"""
  <div class="section">
    <div class="section-title">Bookings (cal.com)</div>
    {error_note}
    <div class="card-grid">
      {cards}
    </div>
  </div>"""


def _section_infra_html(gh: dict) -> str:
    if gh.get("error") and not gh.get("workflows"):
        err_note = f'<p class="api-error">GitHub: {gh["error"]}</p>'
        return f'<div class="section"><div class="section-title">Infrastructure Health</div>{err_note}</div>'

    workflows = gh.get("workflows") or []

    rows = "".join(
        _check_row(
            wf["name"],
            wf["status"] == "success",
            _link(wf.get("url", ""), f'Last run {wf.get("run_at") or "—"}'),
        )
        for wf in workflows
    )

    error_note = f'<p class="api-error">{gh["error"]}</p>' if gh.get("error") else ""

    return f"""
  <div class="section">
    <div class="section-title">Infrastructure Health</div>
    {error_note}
    <div class="check-list">
      {rows}
    </div>
  </div>"""


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def _theme_vars(colors: dict) -> str:
    """Build a :root CSS custom-property block from a brand's colors dict.

    `colors` comes from brand.yaml (a required field). Any missing sub-key falls
    back to a neutral, non-brand default so a partial config still renders.
    """
    primary = colors.get("primary", "#0ea5e9")
    return f""":root {{
      --primary: {primary};
      --primary-dark: {colors.get("primary_dark", primary)};
      --accent: {colors.get("accent", "#dc2626")};
      --heading: {colors.get("heading", "#1f2937")};
      --text: {colors.get("text", "#111827")};
      --gray-bg: {colors.get("gray_bg", "#f8fafc")};
    }}"""


def _render_html(
    generated_at: str,
    published_count: int,
    approved_count: int,
    queued_count: int,
    held_count: int,
    last_published,
    next_approved: list,
    next_queued: list,
    draft_count: int,
    social: dict,
    buffer_sent_count: int,
    buffer_pending: list,
    tokens: list,
    brand_slug: str,
    colors: dict,
    title: str,
    refresh_url: str,
    staging_site_url: str,
    article_url_base: str,
    marketing_repo: str,
    # Phase 2
    ml_stats: dict | None = None,
    buf_stats: dict | None = None,
    quality_gate: dict | None = None,
    # Phase 3
    ga4_stats: dict | None = None,
    gsc_stats: dict | None = None,
    # Phase 4
    cal_stats: dict | None = None,
    gh_stats: dict | None = None,
    # Phase 5
    seo: dict | None = None,
) -> str:

    # --- Section 1: Content Pipeline ---
    if last_published:
        slug = last_published["slug"]
        live_url = f"{article_url_base.rstrip('/')}/{slug}" if article_url_base else ""
        last_pub_detail = f"{_link(live_url, slug)}<br>{last_published.get('published_at', '—')}"
    else:
        last_pub_detail = "No articles published yet"

    social_total = social["linkedin"] + social["x"]
    social_detail = f"{social['linkedin']} LinkedIn · {social['x']} X/Twitter generated"

    pending_count = len(buffer_pending)
    pending_detail = (_slug_list([{"slug": s} for s in buffer_pending], limit=3)
                      if buffer_pending else "None pending")

    # Approved card — green if any approved, amber warning if cron is imminent and none
    if approved_count:
        approved_detail = _slug_list(next_approved, limit=3)
        approved_border = "var(--primary)"
    else:
        approve_cmd = f"python3 -m pipeline.queue_manager --brand {brand_slug} approve &lt;slug&gt;"
        approved_detail = (
            f"<span style='color:#b45309'>Nothing approved — cron won't publish.</span><br>"
            f"<code style='font-size:0.68rem;color:#666'>{approve_cmd}</code>"
        )
        approved_border = "#d97706"

    # Needs Approval card — shows queued items waiting for Kevin
    if queued_count:
        needs_detail = _approve_list(next_queued, staging_site_url, marketing_repo, brand_slug, limit=3)
        needs_border = "#d97706"
    else:
        needs_detail = "All articles approved or on hold"
        needs_border = "var(--primary)"

    # Drafts in staging — link to the GitHub drafts directory
    drafts_url = (
        f"https://github.com/{marketing_repo}/tree/main/brands/{brand_slug}/staging/drafts"
        if marketing_repo else ""
    )
    drafts_detail = _link(drafts_url, "All draft versions across staging/drafts/")

    pipeline_cards = (
        _card("Approved", approved_count,
              f"ready for next cron run (Tue/Thu 8am NZT)<br>{approved_detail}",
              border_color=approved_border, card_id="pipeline-approved-card") +
        _card("Needs Approval", queued_count,
              f"queued, awaiting Kevin sign-off<br>{needs_detail}",
              border_color=needs_border, card_id="pipeline-needs-card") +
        _card("Last Published", "—" if not last_published else "✓",
              last_pub_detail) +
        _card("Drafts in Staging", draft_count, drafts_detail) +
        _card("Social Variants", social_total, social_detail) +
        _card("Buffer Pending", pending_count,
              f"LinkedIn drafts awaiting Kevin approval<br>{pending_detail}",
              border_color="var(--accent)" if pending_count > 5 else "var(--primary)")
    )

    # --- Promote panel ---
    promote_panel = f"""
  <div class="section">
    <div class="section-title">Site: Staging → Production</div>
    <div class="promote-panel">
      <div class="promote-info">
        <p class="promote-desc">Publish changes from staging to production. Review the staging site first, then promote when ready.</p>
        <a class="staging-link" href="{staging_site_url}" target="_blank" rel="noopener">Review staging site →</a>
      </div>
      <button class="promote-btn" id="promote-btn">Promote to Production</button>
    </div>
    <p class="promote-status" id="promote-status"></p>
  </div>"""

    # --- Token Expiry ---
    token_cards = "".join(_token_card(t) for t in tokens)

    # --- Phase 2: Newsletter + Social ---
    newsletter_section = _section_newsletter_html(ml_stats) if ml_stats is not None else ""
    social_section = (
        _section_social_html(buf_stats, quality_gate, buffer_pending)
        if buf_stats is not None else ""
    )

    # --- Phase 3: GA4 + Search Console ---
    ga4_section = _section_ga4_html(ga4_stats) if ga4_stats is not None else ""
    gsc_section = _section_gsc_html(gsc_stats) if gsc_stats is not None else ""

    # --- Phase 4: cal.com + GitHub ---
    infra_section = _section_infra_html(gh_stats) if gh_stats is not None else ""
    bookings_section = _section_bookings_html(cal_stats) if cal_stats is not None else ""

    # --- Phase 5: SEO/GEO ---
    seo_section = _section_seo_html(seo) if seo is not None else ""

    # --- Coming soon (only phases not yet active) ---
    coming_soon_items = []
    if ga4_stats is None:
        coming_soon_items.append(_coming_soon_card("GA4 + Search Console", "Phase 3 · needs Google service account"))
    coming_soon = "".join(coming_soon_items)

    phases_live = "1–4, 5" if ga4_stats is not None else "1–2, 4–5"
    footer_note = f"Phase {phases_live} live"

    refresh_link = (
        f'&nbsp;·&nbsp;<a href="{refresh_url}" target="_blank" rel="noopener">Run manually ↗</a>'
        if refresh_url else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="icon" href="favicon.png" sizes="32x32" />
  <style>
    {_theme_vars(colors)}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--gray-bg); color: var(--text); line-height: 1.5; }}

    header {{ background: var(--primary); color: white; padding: 18px 0; }}
    .header-inner {{ max-width: 1100px; margin: 0 auto; padding: 0 24px;
                     display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }}
    header h1 {{ font-size: 1.25rem; font-weight: 700; letter-spacing: -0.01em; }}
    .header-meta {{ font-size: 0.8rem; opacity: 0.88; }}
    .header-meta a {{ color: white; }}

    main {{ max-width: 1100px; margin: 0 auto; padding: 28px 24px; }}

    .section {{ margin-bottom: 36px; }}
    .section-title {{ font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
                      letter-spacing: 0.1em; color: var(--primary); margin-bottom: 14px; }}

    /* Pipeline cards */
    .card-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 14px; }}
    .card {{ background: white; border-radius: 8px; padding: 18px 20px;
             box-shadow: 0 1px 4px rgba(0,0,0,0.07); border-top: 3px solid var(--primary); }}
    .card-label {{ font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
                   letter-spacing: 0.06em; color: #999; margin-bottom: 8px; }}
    .card-value {{ font-size: 2.25rem; font-weight: 700; color: var(--heading);
                   line-height: 1; margin-bottom: 10px; }}
    .card-detail {{ font-size: 0.78rem; color: #666; }}
    .card-detail a {{ color: var(--primary); text-decoration: none; }}
    .card-detail a:hover {{ text-decoration: underline; }}

    /* Slug lists */
    .slug-list {{ list-style: none; margin-top: 4px; }}
    .slug-list li {{ font-family: ui-monospace, 'SFMono-Regular', Consolas, monospace;
                     font-size: 0.73rem; color: #555; padding: 2px 0;
                     white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .slug-list li::before {{ content: "→ "; color: var(--primary); }}

    /* Badges */
    .badge {{ display: inline-flex; align-items: center; padding: 2px 9px;
              border-radius: 12px; font-size: 0.72rem; font-weight: 700; white-space: nowrap; }}
    .badge-ok {{ background: #d1fae5; color: #065f46; }}
    .badge-warning {{ background: #fef3c7; color: #92400e; }}
    .badge-danger {{ background: #fee2e2; color: #b91c1c; }}

    /* Token cards */
    .token-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }}
    .token-card {{ background: white; border-radius: 8px; padding: 14px 18px;
                   box-shadow: 0 1px 4px rgba(0,0,0,0.07);
                   display: flex; justify-content: space-between; align-items: center; gap: 16px; }}
    .token-name {{ font-size: 0.83rem; font-weight: 600; color: var(--heading); }}
    .token-date {{ font-size: 0.72rem; color: #999; margin-top: 2px; }}
    .token-days {{ font-size: 0.78rem; font-weight: 700; }}

    /* SEO check list */
    .check-list {{ display: flex; flex-direction: column; gap: 10px; }}
    .check-row {{ background: white; border-radius: 8px; padding: 14px 18px;
                  box-shadow: 0 1px 4px rgba(0,0,0,0.07);
                  display: flex; align-items: center; gap: 16px; }}
    .check-label {{ font-size: 0.85rem; font-weight: 600; color: var(--heading);
                    min-width: 110px; font-family: ui-monospace, 'SFMono-Regular', Consolas, monospace; }}
    .check-detail {{ font-size: 0.78rem; color: #666; flex: 1; }}
    .check-detail a {{ color: var(--primary); text-decoration: none; }}
    .check-detail a:hover {{ text-decoration: underline; }}

    /* Error / note */
    .api-error {{ font-size: 0.75rem; color: #b91c1c; margin-bottom: 10px; }}
    .api-note  {{ font-size: 0.75rem; color: #92400e; margin-bottom: 10px; }}

    /* Trend indicators */
    .trend-up   {{ color: #065f46; font-size: 0.72rem; font-weight: 600; }}
    .trend-down {{ color: #b91c1c; font-size: 0.72rem; font-weight: 600; }}
    .trend-flat {{ color: #999; font-size: 0.72rem; }}

    /* Coming soon */
    .coming-soon-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }}
    .coming-soon {{ background: white; border-radius: 8px; padding: 20px;
                    box-shadow: 0 1px 3px rgba(0,0,0,0.05); text-align: center; }}
    .coming-soon-title {{ font-size: 0.85rem; font-weight: 600; color: #bbb; margin-bottom: 6px; }}
    .coming-soon-phase {{ font-size: 0.72rem; color: #ccc; }}

    /* Two-column layout for tables */
    .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
    .subsection-label {{ font-size: 0.68rem; font-weight: 700; text-transform: uppercase;
                         letter-spacing: 0.08em; color: #bbb; margin-bottom: 10px; }}

    /* Data tables (queries, pages) */
    .data-table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; }}
    .data-table th {{ text-align: left; color: #999; font-weight: 600; font-size: 0.68rem;
                      text-transform: uppercase; letter-spacing: 0.05em;
                      padding: 0 8px 6px 0; border-bottom: 1px solid #eee; }}
    .data-table td {{ padding: 6px 8px 6px 0; border-bottom: 1px solid #f5f5f5; color: #444; }}
    .data-table tr:last-child td {{ border-bottom: none; }}
    .q-cell {{ max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .n-cell {{ text-align: right; white-space: nowrap; color: #555; }}

    /* Traffic source bars */
    .src-list {{ display: flex; flex-direction: column; gap: 8px; }}
    .src-row {{ display: flex; align-items: center; gap: 8px; font-size: 0.78rem; }}
    .src-name {{ min-width: 110px; color: #555; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .src-bar-wrap {{ flex: 1; background: #f0f0f0; border-radius: 4px; height: 8px; }}
    .src-bar {{ display: block; background: var(--primary); border-radius: 4px; height: 8px; min-width: 4px; }}
    .src-count {{ min-width: 40px; text-align: right; color: #777; font-size: 0.72rem; }}

    /* Approve buttons */
    .approve-list .approve-item {{ display: flex; align-items: center; justify-content: space-between; padding: 2px 0; }}
    .approve-slug {{ font-family: ui-monospace, 'SFMono-Regular', Consolas, monospace;
                    font-size: 0.73rem; color: #555; overflow: hidden; text-overflow: ellipsis;
                    white-space: nowrap; flex: 1; }}
    .approve-slug a {{ color: #555; text-decoration: none; }}
    .approve-slug a:hover {{ color: var(--primary); text-decoration: underline; }}
    .approve-slug::before {{ content: "\2192  "; color: var(--primary); }}
    .approve-btn {{ font-size: 0.65rem; font-weight: 700; color: white; background: var(--primary);
                   border: none; border-radius: 4px; padding: 2px 9px; cursor: pointer;
                   margin-left: 8px; white-space: nowrap; flex-shrink: 0; line-height: 1.6; }}
    .approve-btn:hover {{ background: var(--primary-dark); }}
    .approve-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .stage-btn {{ font-size: 0.65rem; font-weight: 700; color: var(--primary);
                  border: 1px solid var(--primary); background: white; border-radius: 4px; padding: 2px 7px;
                  cursor: pointer; margin-left: 6px; white-space: nowrap; flex-shrink: 0; line-height: 1.6; }}
    .stage-btn:hover {{ background: #f0fbfc; }}
    .stage-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}

    /* Promote panel */
    .promote-panel {{ background: white; border-radius: 8px; padding: 20px 24px;
                      box-shadow: 0 1px 4px rgba(0,0,0,0.07); border-top: 3px solid var(--primary);
                      display: flex; align-items: center; justify-content: space-between;
                      gap: 24px; flex-wrap: wrap; }}
    .promote-info {{ flex: 1; min-width: 220px; }}
    .promote-desc {{ font-size: 0.83rem; color: #555; margin-bottom: 8px; }}
    .staging-link {{ font-size: 0.78rem; font-weight: 600; color: var(--primary); text-decoration: none; }}
    .staging-link:hover {{ text-decoration: underline; }}
    .promote-btn {{ font-size: 0.85rem; font-weight: 700; color: white; background: var(--primary);
                   border: none; border-radius: 6px; padding: 10px 22px; cursor: pointer;
                   white-space: nowrap; flex-shrink: 0; transition: background 0.15s; }}
    .promote-btn:hover {{ background: var(--primary-dark); }}
    .promote-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .promote-status {{ font-size: 0.78rem; margin-top: 10px; min-height: 18px; }}
    .promote-status.ok {{ color: #065f46; }}
    .promote-status.err {{ color: #b91c1c; }}

    footer {{ max-width: 1100px; margin: 0 auto; padding: 8px 24px 32px;
              font-size: 0.72rem; color: #bbb; }}

    @media (max-width: 640px) {{
      .card-grid {{ grid-template-columns: 1fr 1fr; }}
      .coming-soon-grid {{ grid-template-columns: 1fr 1fr; }}
      .two-col {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>

<header>
  <div class="header-inner">
    <h1>{title}</h1>
    <div class="header-meta">
      Generated {generated_at} NZT {refresh_link}
    </div>
  </div>
</header>

<main>

  <div class="section">
    <div class="section-title">Content Pipeline</div>
    <div class="card-grid">
      {pipeline_cards}
    </div>
  </div>

  {promote_panel}

  <div class="section">
    <div class="section-title">Token Expiry</div>
    <div class="token-grid">
      {token_cards}
    </div>
  </div>

  {newsletter_section}

  {social_section}

  {infra_section}

  {bookings_section}

  {ga4_section}

  {gsc_section}

  {seo_section}

  <div class="section">
    <div class="section-title">Coming soon</div>
    <div class="coming-soon-grid">
      {coming_soon}
    </div>
  </div>

</main>

<footer>{footer_note}</footer>

<script>
  document.querySelectorAll('.stage-btn').forEach(function(btn) {{
    btn.addEventListener('click', async function() {{
      var slug = this.dataset.slug;
      var previewUrl = this.dataset.previewUrl;
      this.disabled = true;
      this.textContent = '…';
      try {{
        var res = await fetch('/api/preview', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ slug: slug }})
        }});
        var data = await res.json();
        if (res.ok) {{
          this.textContent = 'Previewing…';
          this.style.color = '#065f46';
          this.style.borderColor = '#065f46';
          setTimeout(function() {{ window.open(previewUrl, '_blank'); }}, 2500);
        }} else {{
          this.textContent = 'Preview';
          this.disabled = false;
          alert('Preview failed: ' + (data.error || 'Unknown error'));
        }}
      }} catch (e) {{
        this.textContent = 'Preview';
        this.disabled = false;
        alert('Network error: ' + e.message);
      }}
    }});
  }});

  document.querySelectorAll('.approve-btn').forEach(function(btn) {{
    btn.addEventListener('click', async function() {{
      var slug = this.dataset.slug;
      this.disabled = true;
      this.textContent = '…';
      try {{
        var res = await fetch('/api/approve', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ slug: slug }})
        }});
        var text = await res.text();
        var data = {{}};
        try {{ data = JSON.parse(text); }} catch (_) {{ data = {{ error: 'HTTP ' + res.status + ': ' + text.slice(0, 80) }}; }}
        if (res.ok && data.ok) {{
          this.textContent = 'Approved!';
          this.style.background = 'var(--primary)';
          // Update DOM in-place — static HTML won't reflect queue state on reload
          var item = this.closest('.approve-item');
          if (item) item.remove();
          var needsCard = document.getElementById('pipeline-needs-card');
          if (needsCard) {{
            var needsVal = needsCard.querySelector('.card-value');
            if (needsVal) {{
              var n = parseInt(needsVal.textContent, 10);
              if (!isNaN(n) && n > 0) {{
                needsVal.textContent = n - 1;
                if (n - 1 === 0) {{
                  needsCard.style.borderTopColor = 'var(--primary)';
                  var needsDetail = needsCard.querySelector('.card-detail');
                  if (needsDetail) needsDetail.innerHTML = 'All articles approved or on hold';
                }}
              }}
            }}
          }}
          var approvedCard = document.getElementById('pipeline-approved-card');
          if (approvedCard) {{
            var approvedVal = approvedCard.querySelector('.card-value');
            if (approvedVal) {{
              var a = parseInt(approvedVal.textContent, 10);
              if (!isNaN(a)) {{
                approvedVal.textContent = a + 1;
                approvedCard.style.borderTopColor = 'var(--primary)';
                if (a === 0) {{
                  var approvedDetail = approvedCard.querySelector('.card-detail');
                  if (approvedDetail) approvedDetail.innerHTML = 'ready for next cron run (Tue/Thu 8am NZT)';
                }}
              }}
            }}
          }}
        }} else {{
          this.textContent = 'Failed';
          this.style.background = '#b91c1c';
          alert('Approve failed: ' + (data.error || 'HTTP ' + res.status));
          this.disabled = false;
          this.textContent = 'Approve';
          this.style.background = '';
        }}
      }} catch (e) {{
        this.textContent = 'Failed';
        this.style.background = '#b91c1c';
        alert('Network error: ' + e.message);
        this.disabled = false;
        this.textContent = 'Approve';
        this.style.background = '';
      }}
    }});
  }});

  var promoteBtn = document.getElementById('promote-btn');
  var promoteStatus = document.getElementById('promote-status');
  if (promoteBtn) {{
    promoteBtn.addEventListener('click', async function() {{
      promoteBtn.disabled = true;
      promoteBtn.textContent = 'Promoting…';
      promoteStatus.textContent = '';
      promoteStatus.className = 'promote-status';
      try {{
        var res = await fetch('/api/promote', {{ method: 'POST' }});
        var data = await res.json();
        if (res.ok) {{
          promoteBtn.textContent = 'Promoted!';
          promoteBtn.style.background = 'var(--primary)';
          promoteStatus.textContent = data.message || 'Done.';
          promoteStatus.className = 'promote-status ok';
        }} else {{
          promoteBtn.textContent = 'Promote to Production';
          promoteBtn.disabled = false;
          promoteStatus.textContent = 'Error: ' + (data.error || 'Unknown error');
          promoteStatus.className = 'promote-status err';
        }}
      }} catch (e) {{
        promoteBtn.textContent = 'Promote to Production';
        promoteBtn.disabled = false;
        promoteStatus.textContent = 'Network error: ' + e.message;
        promoteStatus.className = 'promote-status err';
      }}
    }});
  }}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate_dashboard(brand_slug: str, output_path: Path | None = None) -> Path:
    brand = load_brand(brand_slug)
    dash = brand.dashboard or {}
    staging = brand.staging_dir
    today = date.today()

    queue_raw = _load_json(staging / "publish_queue.json") or []
    buffer_sent_raw = _load_json(staging / "buffer_sent.json") or {}

    queue = _queue_stats(queue_raw)
    social = _social_stats(brand.social_dir)
    draft_count = _draft_count(brand.drafts_dir)
    buffer_pending = _buffer_pending(buffer_sent_raw)
    tokens = _token_warnings(today, _parse_token_expiry(dash.get("token_expiry", {})))

    last_published = queue["published"][-1] if queue["published"] else None
    next_approved = queue["approved"][:3]
    next_queued = queue["queued"][:3]
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Brand dashboard config
    marketing_repo = dash.get("marketing_repo", "")
    staging_site_url = (dash.get("staging_site_url") or "").rstrip("/")
    title = dash.get("title") or f"{brand.display_name} Dashboard"
    infra_workflows = dash.get("infra_workflows") or []
    refresh_url = (
        f"https://github.com/{marketing_repo}/actions/workflows/deploy-dashboard.yml"
        if marketing_repo else ""
    )

    # Phase 2: MailerLite + Buffer
    ml_api_key = os.environ.get("MAILERLITE_API_KEY")
    buf_token = os.environ.get("BUFFER_ACCESS_TOKEN")
    ml_stats = _fetch_mailerlite_stats(ml_api_key)
    buf_stats = _fetch_buffer_stats(buf_token, brand, buffer_sent_raw)
    quality_gate = _quality_gate_status(queue_raw)

    # Phase 3: GA4 + Search Console
    google_token, google_err = _google_access_token(os.environ.get("GOOGLE_OAUTH_JSON"))
    analytics_cfg = brand.analytics
    ga4_property_id = analytics_cfg.get("ga4_property_id", "")
    gsc_site_url = analytics_cfg.get("gsc_site_url", "")
    if google_err:
        ga4_stats: dict | None = {"error": google_err, "sessions_7d": None, "sessions_28d": None, "sessions_28d_prior": None, "top_pages": [], "sources": []}
        gsc_stats: dict | None = {"error": google_err, "clicks_28d": None, "impressions_28d": None, "avg_position": None, "clicks_28d_prior": None, "impressions_28d_prior": None, "avg_position_prior": None, "top_queries": [], "ctr_opportunities": []}
    elif ga4_property_id and gsc_site_url:
        ga4_stats = _fetch_ga4_stats(google_token, ga4_property_id)
        gsc_stats = _fetch_gsc_stats(google_token, gsc_site_url)
    else:
        ga4_stats = None
        gsc_stats = None

    # Phase 4: cal.com + GitHub infrastructure
    cal_api_key = os.environ.get("CAL_API_KEY")
    gh_token = (
        os.environ.get("DASHBOARD_GH_TOKEN")
        or os.environ.get("CASHBUCKET_GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    cal_stats = _fetch_calcom_stats(cal_api_key) if cal_api_key else {"bookings_7d": None, "bookings_30d": None, "error": None}
    gh_stats = (
        _fetch_github_infra_stats(gh_token, marketing_repo, infra_workflows)
        if (gh_token and marketing_repo and infra_workflows)
        else {"workflows": [], "error": None}
    )

    # Phase 5: SEO/GEO
    seo = _seo_health(brand.site_url)

    html = _render_html(
        generated_at=generated_at,
        published_count=len(queue["published"]),
        approved_count=len(queue["approved"]),
        queued_count=len(queue["queued"]),
        held_count=len(queue["held"]),
        last_published=last_published,
        next_approved=next_approved,
        next_queued=next_queued,
        draft_count=draft_count,
        social=social,
        buffer_sent_count=len(buffer_sent_raw),
        buffer_pending=buffer_pending,
        tokens=tokens,
        brand_slug=brand_slug,
        colors=brand.colors,
        title=title,
        refresh_url=refresh_url,
        staging_site_url=staging_site_url,
        article_url_base=brand.article_url_base,
        marketing_repo=marketing_repo,
        ml_stats=ml_stats,
        buf_stats=buf_stats,
        quality_gate=quality_gate,
        ga4_stats=ga4_stats,
        gsc_stats=gsc_stats,
        cal_stats=cal_stats,
        gh_stats=gh_stats,
        seo=seo,
    )

    if output_path is None:
        out_rel = dash.get("output_dir") or f"workers/{brand_slug}-dashboard/public"
        out_dir = consumer_root() / out_rel
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / "index.html"

    output_path.write_text(html, encoding="utf-8")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Generate a brand's marketing dashboard HTML.")
    parser.add_argument("--brand", default="cashbucket", help="Brand slug (default: cashbucket)")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output path (default: brand's dashboard.output_dir)")
    args = parser.parse_args()

    out = generate_dashboard(args.brand, args.output)
    print(f"Dashboard generated: {out}")


if __name__ == "__main__":
    main()
