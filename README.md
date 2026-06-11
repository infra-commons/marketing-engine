# infra-commons/marketing-engine

Shared Python pipeline for multi-brand article drafting, compliance gating, and publishing.
Entity marketing repos keep only `brands/{name}/` config; all pipeline logic lives here.

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

## Scripts

| Script | Purpose |
|---|---|
| `loop.sh` | Main autoloop entry point |
| `run-once.sh` | Process one item from the queue |
| `run-batch.sh` | Process a batch of queue items |
| `queue-runner.sh` | Queue management helper |

## Requirements

- Python 3.12
- `anthropic>=0.106.0`
- `pyyaml>=6.0.3`

## Brand config reference

See `pipeline/brand_loader.py` → `BrandConfig` dataclass for all required fields.
