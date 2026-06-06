# Changelog

All notable changes to BandiRadar are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

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

[0.1.0]: https://github.com/mayai-it/bandiradar/releases/tag/v0.1.0
