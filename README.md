# infra-commons/marketing-engine

Shared Python pipeline for multi-brand article drafting, compliance gating, and publishing.
Entity marketing repos keep only `brands/{name}/` config; all pipeline logic lives here.

## Consuming the engine as a submodule

When this repo is vendored at a brand repo's root, no configuration is needed.
When it is consumed as a git submodule (e.g. at `engine/`), the engine cannot
infer the brand repo's location from its own path, so the consumer **must**
export the brand repo root before invoking any pipeline script:

```bash
export MARKETING_REPO_ROOT=/path/to/your/marketing  # where brands/ lives
```

`brand_loader.consumer_root()` reads this (falling back to the engine root for
the legacy vendored layout). It locates `brands/` and the sibling site checkout;
the engine's own base modules (`phrase_banks.py`, `banned_phrases.py`) are always
resolved relative to the engine itself.

## How to add a new brand

1. In your entity's marketing repo, create `brands/{slug}/`:

   ```
   brands/{slug}/
   ├── brand.yaml        # Brand identity, site config, secrets env-var names, CTA copy
   ├── phrase_banks.py   # Brand-specific phrase lists
   ├── nav.html          # Site nav HTML snippet
   └── footer.html       # Site footer HTML snippet
   ```

2. Use `brands/rolliq/brand.yaml` (in `rolliq-com/marketing`) as a template — update all fields.

3. Use `brands/rolliq/phrase_banks.py` as a template — implement the same functions.

4. Set the `BRANDS_DIR` path in `pipeline/brand_loader.py` to point at your entity repo's
   `brands/` directory, or symlink `brands/` into the pipeline working directory.

5. Copy `constitution.md.template` → `constitution.md` and fill in brand-specific details.

6. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

7. Run tests to confirm the pipeline is healthy:

   ```bash
   pytest tests/
   ```

## Pipeline modules

| Module | Purpose |
|---|---|
| `pipeline/brand_loader.py` | Loads `brand.yaml` + `phrase_banks.py` from `brands/{slug}/` |
| `pipeline/draft_generator.py` | Generates article drafts via Claude API |
| `pipeline/compliance_gate.py` | Hard stop — enforces content quality rules |
| `pipeline/signal_scrubber.py` | Removes AI tells and style violations |
| `pipeline/queue_manager.py` | Manages the publish queue |
| `pipeline/publisher.py` | Publishes approved articles to the entity site |
| `pipeline/dashboard_generator.py` | Renders a brand's metrics dashboard HTML (see below) |

## Metrics dashboard

`pipeline/dashboard_generator.py` renders a single static `index.html` summarising a
brand's marketing state (content pipeline, token expiry, MailerLite, Buffer, GA4,
Search Console, cal.com, GitHub workflow health, SEO/GEO). It is brand-agnostic:
theme colours, title, output path, repo slugs, token-expiry list and infra
workflows all come from the brand's `brand.yaml`. Each data source degrades
gracefully when its secret/config is absent.

```bash
export MARKETING_REPO_ROOT=/path/to/your/marketing
PYTHONPATH=engine python3 -m pipeline.dashboard_generator --brand {slug} [--output path/index.html]
```

A brand opts in by adding a `dashboard:` block to its `brand.yaml` (brands without
one render no dashboard). The block also reuses the existing `colors:`,
`article_url_base:` and `analytics:` fields:

```yaml
dashboard:
  title: "Acme Dashboard"
  marketing_repo: acme-com/marketing            # used for GitHub source links
  output_dir: workers/acme-dashboard/public     # relative to MARKETING_REPO_ROOT
  staging_site_url: https://acme-staging.example.dev
  infra_workflows:                              # [workflow_file, display_name]
    - [auto-publish.yml, auto-publish]
    - [deploy-dashboard.yml, deploy-dashboard]
  token_expiry:                                 # label -> YYYY-MM-DD
    BUFFER_ACCESS_TOKEN: "2026-08-30"
```

Article items link out: published/last-published → live `{article_url_base}/{slug}`;
queued items → GitHub source under `brands/{slug}/`; drafts → the GitHub drafts dir.
Secrets are read from env: `MAILERLITE_API_KEY`, `BUFFER_ACCESS_TOKEN`,
`GOOGLE_OAUTH_JSON`, `CAL_API_KEY`, and a GitHub token
(`DASHBOARD_GH_TOKEN` / `GITHUB_TOKEN`).

## Scripts

| Script | Purpose |
|---|---|
| `loop.sh` | Main autoloop entry point |
| `run-once.sh` | Process one item from the queue |
| `run-batch.sh` | Process a batch of queue items |
| `queue-runner.sh` | Queue management helper |

## Two-stage weekly cadence (Tuesday draft → sign-off → Thursday publish)

To stop unreviewed content publishing automatically, the article cadence is split into
two scheduled stages with an explicit human sign-off gate between them:

```
 Tuesday                          human                      Thursday
 ───────                          ─────                      ────────
 draft_pipeline  ── enqueues ──▶  queue_manager approve ──▶  auto-publish
 (topic → brief                   (the sign-off gate)        (publishes ONLY
  → draft → queue                                             'approved' items)
  as 'queued'/held)
```

**Stage 1 — Tuesday (`pipeline.draft_pipeline`).** One unattended command:

```bash
python3 -m pipeline.draft_pipeline --brand <slug>
```

1. **Topic selection** (`pipeline.topic_selector`) — picks a timely topic from the brand's
   trending feeds and synthesizes a full article brief. Explainable + overridable:
   feeds come from `brand.yaml` (`topic_feeds:`, defaulting to NZ-SME business-news
   Google-News RSS); every run logs the candidates + rationale to
   `staging/topics/topic-<date>.json`; an operator can override with `--topic "…"`,
   swap feeds with `--feeds url,url`, or reuse an existing brief with `--brief brief-011`.
2. **Draft generation** (`pipeline.draft_generator`) — writes a gated draft to `staging/drafts/`.
3. **Enqueue held** (`queue_manager add`) — registers the draft as `status: "queued"`.
   A `queued` item is **not publishable** (see the gate below).

**The sign-off gate.** Publishing requires the **status** approval model
(`workflow.approval: status` in `brand.yaml`). Under it, `queue_manager next` — the
selector the publish workflow calls — returns an item **only** when its status is
`approved`. A human advances an item across the gate with:

```bash
python3 -m pipeline.queue_manager --brand <slug> approve <slug>
```

You can assert the gate holds — a `queued` draft is withheld, an `approved` one is
released — with `pytest tests/test_two_stage_gate.py`.

**Stage 2 — Thursday (`auto-publish.yml` in the consumer marketing repo).** The scheduled
publish workflow runs `queue_manager next` (which yields only approved items) and publishes
it via `pipeline.publisher`. A draft never approved is simply not published.

**Scheduling** lives in the consumer marketing repo's GitHub Actions: `generate-draft.yml`
targets Tuesday (`cron: '0 20 * * 1'`), `auto-publish.yml` targets Thursday
(`cron: '0 20 * * 3'`) — Mon/Wed 20:00 UTC = Tue/Thu 08:00 NZST.

## Requirements

- Python 3.12
- `anthropic>=0.106.0`
- `pyyaml>=6.0.3`

## Brand config reference

See `pipeline/brand_loader.py` → `BrandConfig` dataclass for all required fields.
