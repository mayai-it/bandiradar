# Changelog

All notable changes to BandiRadar are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [0.5.1] — 2026-06-11 — Honest LLM client status

### Fixed
- **No more fake "LLM ON".** With `ANTHROPIC_API_KEY` set, the monitor declared LLM
  scoring active even when the `anthropic` SDK was not installed (`uv sync` without
  `--extra anthropic`), so `matching/llm.get_client()` returned `None` silently and
  scoring fell back to the heuristic under the (invalid-for-heuristic) `--mode
  balanced`, with toscana failing on a misleading "requires provider + key".
  - The monitor workflow now installs `uv sync --extra anthropic` when the key is
    present, and a guard step fails the run immediately (with the honest reason) if
    the key is set but no LLM client can be built — never a silent fallback.
  - New `matching.llm.client_status()` (a pure diagnostic alongside `get_client`,
    NOT changing its `→ None` fallback contract) reports WHY the client is unavailable,
    distinguishing "no provider/key configured" from "`<provider>` SDK not installed
    — run: uv sync --extra `<provider>`". It drives the LLM-scraper error message and
    `doctor`'s note.
  - `STATUS.md`'s `Mode` line (and the `flagged`-vs-`drift` recipe state) now reflect
    the REAL client via `monitor_status.py --llm-active` (set by the workflow only
    after the guard verifies it), not mere key presence.

## [0.5.0] — 2026-06-11 — Daily self-maintaining monitor

### Added
- **Live monitor** (`.github/workflows/monitor.yml`) — a daily GitHub Actions run
  (cron `0 6 * * *` + manual dispatch) that fetches every key-less source (plus
  `toscana`, so the self-healing crawl drift-check runs in production), watches
  every bundled profile, and force-pushes RSS/JSON feeds + `STATUS.md` as a single
  flat commit to an orphan `monitor-data` branch. **Zero secrets** (guardrail 1):
  keyless ⇒ recall mode + offline heuristic + drift detect-only; the optional
  `ANTHROPIC_API_KEY` secret ⇒ LLM scoring + the crawl healer. The run fails only
  when EVERY source fails; partial failures are warnings in `STATUS.md`.
- **`scripts/monitor_status.py`** — pure, offline `STATUS.md` generator: per-source
  outcome/counts from the `runs` table, new-match counts from the feed JSONs, and
  crawl-recipe state (`ok`/`drift`/`healed`/`flagged`) from `crawl_recipes` +
  `crawl_golden` + the live `doctor --json` crawl-health. Tested in
  `tests/test_monitor_status.py`.
- `run_watch(fetch=False)` / `watch --skip-fetch` — skip the live fetch and match
  the data already in the DB. The per-profile delta stays correct (it is computed
  from the store's change-detection against each profile's own marker, not from the
  fetch result). Lets a multi-profile run fetch every source ONCE and reuse it.
- Identifying `User-Agent` on every live request (`http.client()` factory:
  `bandiradar/<version> (+repo)` + a fail-fast 10s connect timeout), plus a
  `blocked` structured `FetchErrorKind` (401/403/451) via `http.raise_for_status`,
  so a server refusal reads distinctly from an outage.

### Changed
- `watch --rss PATH --json` now keeps **stdout pure JSON** — the "wrote RSS feed"
  confirmation is routed to stderr when `--json` is set — so one invocation writes
  both the RSS file and a valid JSON feed (presentation-only; no business logic in
  `cli.py`).
- The monitor workflow fetches **once per run** (first profile fetches, the rest
  `--skip-fetch`), tees watch stderr to the Actions log, adds `timeout-minutes: 60`,
  and runs a pre-flight reachability probe of the TED + incentivi endpoints.

### Fixed
- **Monitor run time ~30+ min → ~5 min.** Every `watch` re-fetched all sources for
  each of the 8 profiles; now the run fetches once and the rest reuse the DB.
- **TED 403 from CI** is no longer a generic `unknown` error: the honest User-Agent
  rules out a UA block, and a persistent (datacenter-IP) 403 is classified `blocked`
  and shown as such in `STATUS.md` while every other source still runs.
- **Slow-source blowups capped.** A short connect timeout + a cumulative
  `DEFAULT_MAX_ELAPSED` budget in `with_retry`/`stream_with_retry` stop a
  timing-out host (e.g. incentivi `ConnectTimeout`) from burning minutes of retries.

## [0.4.0] — 2026-06-09 — Coverage & self-healing

### Added
- `anac_pvl` — live feed of OPEN Italian public tenders (ANAC Pubblicità a Valore
  Legale): public JSON API, NO credentials, incl. sub-threshold gare TED never
  lists; keeps only still-open gare. The live open-calls feed the engine lacked.
- CPV resolver — PVL Italian CPV labels -> official 8-digit EU CPV codes (packaged
  vocabulary), lighting the prefix-gate; measured +0.18 keyless recall on tender
  profiles at zero FPR cost.
- Region fallback — province -> comune (ISTAT) -> buyer -> national.
- Coverage map (docs/coverage-map.md) — honest landscape of Italian funding data:
  open feeds vs gated, with the honest gap.
- Self-healing crawl — generic spine (crawl recipes as DATA + drift detection +
  golden-sample validator) + an LLM healer: on listing drift an LLM re-derives the
  crawl recipe; adopted ONLY if it exactly reproduces the last-good refs, else
  flagged for a human. First demonstrated on the Toscana scraper.

### Changed
- Source inventory: 6 key-less live sources + 1 LLM-assisted scraper.
- Eval corpus grows to ~312 labelled opportunities (adds real PVL open tenders).

### Notes
- Some commits carry aspirational tags (feat(0.6.0)); this 0.4.0 consolidates all
  work since 0.3.0.

## [0.3.0] — 2026-06-09 — Matching quality

Makes matching quality **measurable and tunable**. Backward-compatible — the
default suite stays fully offline / zero-secret; programmatic callers are unchanged.

### Added
- Labelled evaluation harness (`bandiradar eval`) over a shipped 292-opportunity /
  8-profile gold set: precision@5/@10, recall, FPR — per profile + macro-aggregate.
- `eval --diagnostics`: recall attribution (prefilter-drop vs below-k vs gate-level),
  min_score threshold sweep, full-text A/B — all free (no extra scoring).
- Operating-point modes: `--mode {precision|balanced|recall}` on match/watch/batch
  + MCP `search_opportunities`, mapping to min_score cutoffs (40 / 20 / 0).
  Default `balanced` at the CLI/MCP layer; `run_match`'s programmatic default stays
  recall (min_score 0) — backward-compatible.
- "Matching quality (measured)" README section: reproducible heuristic-vs-LLM numbers
  + honest limits.
- `seeks` profile dimension (grant vs tender bidder); deterministic gold corrections
  (geo / instrument / seeks) via `scripts/correct_gold.py`.
- Optional, OFF by default, both measured: embeddings semantic prefilter
  (`embeddings` extra — net-negative at the current recall ceiling) and listwise
  reranking (`eval --rerank` — cheaper top-k, loses calibrated thresholding).

### Changed
- CLI/MCP default operating point is now `balanced` (min_score 20). Programmatic
  callers unchanged.

## [0.2.0] — 2026-06-06 — Reliability

Hardens live fetching and observability. Backward-compatible — existing SQLite DBs
upgrade in place; the default suite stays fully offline / zero-secret.

### Per-source isolation & observability
- `watch` / `batch` (via the new `run_fetch_many`) run each source independently:
  **one source failing never aborts the others.**
- Every fetch returns a structured per-source result (status `ok`/`partial`/
  `failed`/`empty`, counts, `error_kind`, duration) that is **returned, persisted**
  (one `runs` row per source), **and logged.**
- A stdlib **logging foundation**: one logger per module, `-v/--verbose` for DEBUG,
  per-page progress as log records, no secrets logged.

### Correctness
- Lifecycle `status` (open/closing_soon/closed) is now **computed at read time**
  from `deadline` + now — no more stale "open" past a deadline.
- **Structured error kinds** (rate_limited / unavailable / invalid / unknown) drive
  meaningful **exit codes** (3 / 4 / 2 / 1) — no string-matching.
- The "changed/amended" signal is **decoupled from lifecycle status** (tracked via
  `version` + `updated_at`, surfaced by `list_new` + the watch marker); `status` is
  now purely lifecycle.

### Diagnostics
- **`bandiradar doctor`** — a per-source reachability probe (bounded `limit=1`,
  isolated, into a throwaway in-memory DB) plus environment checks (LLM config,
  optional extras, DB migrates cleanly, Python version), as a human table or
  `--json`, with a health-based exit code. Makes **no LLM call**; key-dependent
  sources report "needs key" rather than failing.

### Tests & upgrades
- **Contract tests** drive each source's real `fetch()` against a recorded response
  (envelope included) in CI — pinning fetch+parse to reality.
- An opt-in **live drift check** (`uv run pytest -m live`) hits real endpoints on
  demand (never in CI).
- DBs **upgrade cleanly** via a PRAGMA-introspecting migration (upgrade path tested).

### Known limitations / next
- 0.1.0's reliability gaps are now **closed**: per-source isolation, read-time
  status recompute, the amended/lifecycle split, and contract tests are all done.
- Matching is still **lexical** (CPV-prefix + keyword/capability overlap + optional
  LLM rerank); **0.3.0** targets semantic (embeddings) matching quality.
- Then **0.4.0** broadens source coverage, and **0.5.0** grows the intelligence
  track. Live-fetch hardening continues (deep-pagination of very large sources;
  more recorded cassettes as APIs drift).

## [0.1.0] — 2026-06-06

First public release: the open-core engine that monitors Italian public funding
opportunities, normalizes them into one canonical model, and ranks them against a
company profile. Runs **fully offline on `--sample` with zero secrets**, and is on
PyPI — `pip install bandiradar` (a CI job installs the built wheel and runs
`--sample` end to end, so the bundled data ships intact).

### Engine
- Canonical `Opportunity` / `RawDoc` / `Profile` / `Match` model (the contract).
- Pluggable `Source` framework with a self-registering registry.
- Two-stage matcher: a pure, explainable deterministic prefilter, then optional
  LLM relevance scoring (Stage 2) with a **deterministic zero-secrets offline
  fallback** — the LLM is never required.
- SQLite storage with dedupe + `content_hash` change detection (a changed notice
  becomes re-notifiable / `amended`), and a clean schema **upgrade path**
  (existing DBs migrate columns before indexing — no crash on upgrade).

### Sources
- **TED** (EU tenders), **incentivi.gov.it** (national incentives),
  **Regione Lombardia** (Socrata) and **Regione Lazio** (LazioInnova WP-REST) —
  all live and **key-less**.
- **Regione Toscana** — an **LLM-assisted scraper** for portals with no field API
  (live fetch needs an LLM key; `--sample` replays a recorded extraction offline).
- **ANAC OCDS** — live, key-less, **historical / awarded-contracts** feed (mostly
  closed; useful for market/history analysis, not as a feed of open calls).

### Intelligence & enrichment
- ANAC historical **benchmarks** (value/volume/seasonality by CPV division) and
  optional matcher **enrichment** (`--with-benchmarks`).
- Optional **PDF document enrichment** (folds attachment text into matching;
  optional OCR extra for scanned PDFs).

### Interfaces
- `watch` monitor loop (new/amended deltas) with **JSON / RSS** export.
- A thin **CLI** and an **MCP server** (drive it from a shell or an AI agent).

### Live-fetch resilience
- **Retries with exponential backoff** on transient failures (HTTP 429, 5xx,
  timeouts, connection errors), honoring `Retry-After`; a clear error when
  exhausted. Verified live against TED and Lombardia.
- **Pagination safety cap + progress**: `--limit` / `--max-pages` and a default
  cap so no source runs unbounded; per-page progress to stderr.
- **Progressive save**: records are upserted as they stream in, so a mid-fetch
  failure keeps everything already saved and records the run as `partial` with the
  error (no lost work, no raw traceback).

### Known limitations / next (0.2.x)
- `watch`/`batch` fetch all sources together; per-source failure isolation is not
  yet implemented (one failing source can mark the combined run partial).
- Opportunity `status` is computed at ingestion and not recomputed on an unchanged
  record, so a stored `open` item can age past its deadline until re-fetched.
- Live-source coverage is exercised with mocked clients + a couple of real runs;
  richer contract tests against live responses are still to come.
- `amended` is sticky until the opportunity is re-derived — no acknowledge/reset
  flow yet (a delivery concern, partly lives in the private `bandiradar-pro`).

[0.2.0]: https://github.com/mayai-it/bandiradar/releases/tag/v0.2.0
[0.1.0]: https://github.com/mayai-it/bandiradar/releases/tag/v0.1.0
