# CLAUDE.md — bandiradar (open core)

Internal status, roadmap and strategy live in ROADMAP.local.md (gitignored, local only — read it first if present).

Project memory for coding agents. Read this fully before editing. The deep design
lives in `ARCHITECTURE.md`; this file is the operational contract.

## One-liner
Open-source engine that monitors Italian public funding opportunities (tenders,
grants, incentives), normalizes them into one canonical `Opportunity` model, and
ranks them against a company `Profile` with a two-stage matcher.

## What this repo IS / IS NOT
- **IS (open, MIT):** engine, `Source` framework, reference adapters (ANAC, later
  incentivi.gov.it), two-stage matcher, CLI, MCP server.
- **IS NOT (lives in private `bandiradar-pro`):** web dashboard, premium/regional
  adapters, delivery channels (WhatsApp/email/alerts), multi-tenant, hosting.
- Boundary rule: single-user/local = here. Managed/multi-client/delivery = pro.
- **Never** add dashboard, paid, or delivery-channel code to this repo.

## Architecture map (module → responsibility)
```
src/bandiradar/
  models.py        # pydantic: Opportunity, RawDoc, Profile, Match. THE contract.
  sources/
    base.py        # Source Protocol + registry
    anac.py        # ANAC/PNCP (OCDS) historical adapter + fixture
    anac_pvl.py    # ANAC PVL — OPEN tenders, live, no-creds + fixture
    heal.py        # LLM crawl-recipe healer (re-derives a drifted recipe)
    llm_scraper.py # reusable LLM HTML extractor; re-exports the crawl spine
  cpv.py           # CPV Italian-label → 8-digit code resolver (pure, offline)
  crawl.py         # self-healing crawl spine (stdlib: recipes + drift + golden)
  recipe_store.py  # per-source CrawlRecipe overrides + golden (CONFIG, not code)
  matching/
    prefilter.py   # Stage 1: pure deterministic filter
    relevance.py   # Stage 2: LLM scorer (+ offline fallback + cache)
    llm.py         # provider-agnostic LLM client
    prompts.py     # prompt templates
  storage.py       # SQLite store, dedupe, change detection
                   #   + crawl_recipes + crawl_golden tables (self-healing crawl)
  core.py          # service layer that orchestrates the pipeline
  evaluation.py    # matching-quality eval: pure metrics + run over eval corpus
  cli.py           # Typer CLI (thin)
  mcp_server.py    # FastMCP server (thin)
  resources.py     # importlib.resources access to packaged data (below)
  data/            # PACKAGED runtime data (ships in the wheel)
    fixtures/      # recorded source payloads for offline tests / --sample
    profiles/      # example profiles (mayai.yaml, manifattura.yaml, …)
    eval/          # labelled eval set: opportunities.jsonl + gold.yaml
    cpv_it.json    # official EU CPV 2008 vocabulary (Italian label → code)
    comuni_it.json # ISTAT comuni table (region resolution for anac_pvl)
tests/
scripts/
  monitor_status.py    # live-monitor STATUS.md generator (pure, offline, tested)
.github/workflows/
  monitor.yml          # daily self-maintaining monitor (keyless; key→LLM+healer)
```
Runtime data lives INSIDE the package and is reached via `bandiradar.resources`
(importlib.resources) — never `Path(__file__).parents[...]` — so `--sample` and the
bundled example profiles work from a pip-installed wheel, not only a checkout.
Interfaces (`cli.py`, `mcp_server.py`) are THIN — no business logic. All logic
lives in `core.py`, `sources/`, `matching/`, `storage.py`.

## Sources (7)
`anac_pvl`, `ted`, `incentivi`, `anac`, `lombardia`, `lazio` are **key-less** (no
credentials, public APIs/feeds); `toscana` is an **LLM scraper** (HTML portal, no
clean data API — uses `sources/llm_scraper.py` + the self-healing crawl spine).
Note the two ANAC adapters are complementary, not duplicates:
- **`anac_pvl`** = ANAC *Pubblicità a Valore Legale* — the **live feed of OPEN
  tenders** (`dataScadenza` in the future), no creds. This is the source of
  currently-biddable gare.
- **`anac`** = ANAC/PNCP **OCDS historical** data — *retrospective* (awarded
  contracts), so mostly-closed; its value is the benchmark/historical track.

## Self-healing crawl
The crawl an LLM scraper depends on (the listing it walks) is the FRAGILE part, so
it's modelled as a `CrawlRecipe` — **DATA, not code** (`crawl.py`, stdlib, no I/O).
`validate_refs` detects DRIFT (the recipe no longer reproduces the live listing).
On drift, the LLM healer (`sources/heal.py`) re-derives a candidate recipe (still
DATA, not a code change) and it is **adopted ONLY if it reproduces the golden
EXACTLY** — a single guarded `recipe_store.adopt()` behind
`crawl.recipe_reproduces_golden()`, the deterministic socket the LLM cannot bypass.
If it doesn't reproduce the golden, the recipe is flagged for human review, never
auto-adopted. Recipes + golden persist in SQLite (`crawl_recipes`, `crawl_golden`)
and the per-source override/golden config lives in `recipe_store.py` (auditable:
`{recipe, adopted_at, reason, validated_by}`). Demo: `scripts/demo_self_heal.py`
(GIF in the README).

## Live monitor (GitHub Actions — self-maintaining)
`.github/workflows/monitor.yml` runs daily (cron `0 6 * * *`) + on demand. It
checks out an orphan **`monitor-data`** branch into `./state/` (created empty if
absent), points the DB at `state/bandiradar.db` (via the existing `BANDIRADAR_DB`
env — already honoured by `storage._default_db_path`), runs `bandiradar watch` for
EVERY bundled profile (all sources incl. `toscana`, so the crawl drift-check runs in
prod), generates `state/STATUS.md`, and force-pushes a SINGLE flat commit to
`monitor-data` (generated state must not bloat history). **Zero secrets** (guardrail
1): keyless ⇒ `--mode recall` + offline heuristic + drift detect-only; the optional
`ANTHROPIC_API_KEY` secret ⇒ LLM scoring + healer active. The run fails (exit≠0)
ONLY if EVERY source failed; partial failures are warnings in `STATUS.md`.

- **One watch invocation writes both feed files.** `watch --rss X --json` writes the
  RSS file AND emits pure JSON to stdout — the "wrote RSS feed" confirmation is
  routed to **stderr** when `--json` is set (cli.py), so the redirected
  `state/feeds/<p>.json` stays valid JSON. Two invocations would NOT work: `watch`
  advances the per-profile marker, so the second would see an empty delta. This is a
  presentation-only fix — NO business logic was added to `cli.py`.
- **`scripts/monitor_status.py` is pure composition** (no network, no engine logic):
  per-source esito/conteggi from the `runs` table (the persisted `SourceResult`),
  new-match counts from each `feeds/<p>.json`, and crawl-recipe state from
  `crawl_recipes`/`crawl_golden` + the live `doctor --json` crawl-health. Recipe
  states: `healed` (override adopted this run) · `drift` (degraded/broken, keyless) ·
  `flagged` (drift + key but heal couldn't reproduce the golden → human) · `ok`.
  Tested offline in `tests/test_monitor_status.py`.

## The canonical model is a contract
`Opportunity` (see `models.py` / `ARCHITECTURE.md §4`) is the superset for
tenders AND grants. Do not break its field names/shape without updating
`ARCHITECTURE.md` and every adapter + test in the same change.

## Commands
```bash
uv sync                      # install
uv run pytest                # tests (must pass offline, no secrets)
uv run ruff check . && uv run ruff format .
uv run bandiradar fetch --source anac --sample   # offline sample run
uv run bandiradar match --profile mayai --sample # --profile: bundled name OR path
uv run bandiradar eval       # matching-quality metrics over the labelled corpus
uv run bandiradar mcp        # start MCP server
# optional semantic prefilter (downloads a model once); measure it:
uv sync --extra embeddings && uv run bandiradar eval --embeddings
# live-monitor status page (offline; reads the run's DB + feeds, no network):
uv run python scripts/monitor_status.py --db state/bandiradar.db \
  --feeds state/feeds --doctor state/doctor.json --profiles mayai,manifattura \
  --out state/STATUS.md
```

## Operating-point modes
`match`/`watch`/`batch` + `core.run_match`/`run_monitor`/`run_batch`/`run_watch` +
MCP `search_opportunities` take `--mode` → a `min_score` cutoff via
`core.MATCH_MODES`: `precision`=40, `balanced`=20 (DEFAULT at the CLI/MCP layer),
`recall`=0. `--min-score N` overrides `--mode`. `core.run_match`'s programmatic
default stays `min_score=0`/`mode=None` (recall) — backward-compatible; only the
CLI/MCP option defaults are `balanced`. Precision points are meaningful WITH an LLM
key (calibrated 0-100 scores); the offline heuristic can't threshold cleanly, so
keyless = recall-oriented. Numbers: see README "Matching quality (measured)".

## Matching evaluation (`bandiradar eval`)
Runs the matcher over the shipped labelled corpus (`data/eval/`, now 312
opportunities — up from 292) for the gold profiles and prints precision@5/@10,
recall, FPR — per profile + macro-aggregate.
Offline by default (heuristic). If an LLM key is set it ALSO reports the LLM on the
SAME gold set. **Label convention:** `borderline` counts as relevant for RECALL but
NON-relevant for PRECISION; `not` are the negatives for FPR. To pin a TRUE heuristic
baseline alongside a configured LLM, pass `client=relevance.HEURISTIC` (NOT
`client=None`, which falls back to the configured client). Gold labels in
`gold.yaml` are AUTO-PROPOSED — a curated starting set for human review, then
hardened by `scripts/correct_gold.py`: deterministic, auditable rule-based fixes
(GEO = wrong-region → not; SEEKS = wrong instrument class for `Profile.seeks`, e.g.
tenders for a grant-only profile → not; INSTRUMENT = debt/equity/non-funding → not
for grant-seekers). The rules are recorded in `gold.yaml`'s `_meta.corrections`; the
script never promotes labels (that stays human). Re-run it to regenerate.

**Diagnostics** (`eval --diagnostics`, free — no extra scoring): *recall
attribution* splits each missed relevant-for-recall item into Stage-1 `prefilter_drop`
(→ embeddings) vs Stage-2 `below_k` (→ reranking); *gate attribution* names WHICH
Stage-1 gate dropped each (so an over-strict gate is told from a real ceiling — on
the corrected gold the 6 drops are 4 genuinely-closed + 2 lexical-gap, i.e. NO
over-strict gate, ceiling is real); a *min_score sweep* prints the
precision/recall/FPR curve across cutoffs.

**Listwise rerank** (`eval --rerank`, `matching/rerank.py`, OFF by default, opt-in):
one comparative LLM call per profile (vs pointwise's N) ranking the whole candidate
set; same prefilter so recall/FPR match pointwise, only the order differs. *Measured:*
lifts top-k precision slightly (P@5 0.37→0.39, P@10 0.24→0.25) at ~12× fewer calls,
BUT its comparative scores don't threshold well — pointwise's calibrated 0-100 wins
the min_score sweep decisively (P@5 0.73 vs 0.49 at thr 40). So keep pointwise for a
high-precision thresholded view; listwise is the cheaper top-k-only option. **Full-text experiment**
(`eval --full-text`, extra scoring): re-scores feeding the UNCAPPED requirements
text (vs the `prompts._MAX_DOC_CHARS` brief) and reports the aggregate delta — a
controlled A/B, threaded via `full_text=` through `run_match`/`score_all`/`score`
and part of the relevance cache key. None of these change default matcher behaviour.

**Embeddings semantic prefilter** (OPTIONAL, OFF by default — the `embeddings`
extra = fastembed/ONNX, no torch). `matching/embeddings.py` adds a hybrid Stage-1
relevance signal: `cpv OR keyword OR cosine ≥ threshold`, injected via
`run_match(embedder=…)`/`prefilter(embedder=…)` (`get_embedder()` → None when the
extra/model is absent, so the default path is unchanged and the test suite never
loads a model — `conftest` forces `BANDIRADAR_EMBEDDINGS=none`; tests use a fake
embedder). Opportunity vectors cache in SQLite (`SqliteEmbeddingCache`, by
content_hash). **Measured & currently NOT enabled:** `eval --embeddings` (offline,
heuristic) showed recall +0.01–0.02 but FPR up and the candidate set 1.2–2.7× —
not net-positive, because the gold corrections already removed most prefilter-drops
(only ~6 remain). Keep the code optional/off; revisit with reranking or a higher
threshold.

## Conventions
- Python 3.12, full type hints, pydantic v2.
- `ruff` for lint + format. `pytest` for tests. `uv` for env/deps.
- Pure functions where possible (esp. Stage-1 prefilter and all `to_opportunities`
  mappers) — they must be unit-testable without I/O.
- No network or API keys required to run the default test suite.
- Small modules, single responsibility, explicit over clever.

## Guardrails (do not violate)
1. **Offline-runnable always.** The repo must build, test, and demo on bundled
   sample data with **zero secrets**. The LLM matcher has a deterministic offline
   fallback when no API key is present.
2. **Privacy.** Send only minimal opportunity text + a compact profile summary to
   the LLM — never raw dumps or full personal data.
3. **No secrets in the repo.** Keys come from env (`.env` is gitignored).
4. **Stage-1 prefilter stays pure** (no LLM, no network).
5. **Every Source ships a fixture + a test.** No adapter without an offline test.
6. **Thin interfaces.** No business logic in `cli.py` / `mcp_server.py`.

## How to add a new Source (playbook)
1. Create `src/bandiradar/sources/<name>.py` implementing the `Source` Protocol.
2. `fetch()` returns `RawDoc`s (HTTP/feed/API). `to_opportunities()` maps raw →
   `Opportunity` (pure).
3. Record a real payload into `src/bandiradar/data/fixtures/<name>.json` (access it
   via `resources.fixture("<name>.json")`, not a `__file__`-relative path).
4. Add `tests/test_<name>.py` asserting the mapper output against the fixture.
5. Register the source in `sources/base.py` registry.
6. Run `uv run pytest` — green before stopping.
(See the `add-a-source` skill for the full template.)

## Definition of done for a slice
- New/changed code has tests; `uv run pytest` is green offline.
- `ruff check` clean.
- `ARCHITECTURE.md` / this file updated if a contract changed.
- The vertical slice runs end-to-end on `--sample`.

## Domain glossary
- **ANAC** — Italian Anticorruption Authority; runs public-contracts data.
- **PNCP** — Piattaforma Nazionale Contratti Pubblici (national public-contracts
  platform; ANAC publishes notices here).
- **OCDS** — Open Contracting Data Standard; JSON schema for tenders. Align tender
  fields to it.
- **CPV** — Common Procurement Vocabulary; procurement category codes on tenders.
- **ATECO** — Italian business activity classification codes (on companies).
- **bando / gara** — a public call / tender.
- **rettifica** — an amendment to a published notice → triggers `amended` status.
- **incentivo / agevolazione** — a business grant/incentive (e.g. Transizione 5.0).
- **SdI** — Sistema di Interscambio (e-invoicing; not used here, avoid confusion).
