# Changelog

All notable changes to BandiRadar are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [0.12.0] — 2026-06-13 — Trust spine: deterministic extraction validation

### Added
- **`trust.py`** — the deterministic gate over every LLM extraction, same
  propose/dispose philosophy as the self-healing crawl: PURE
  `assess(extraction, page_text) -> TrustReport {checks, confidence, verdict}`,
  no I/O/LLM so the model it judges cannot game it. Checks: deadline-in-text
  (Italian date formats + the English Java/CMS-template form + a year-less
  full-month form), amount-in-text (normalized separators/€/verbal multipliers),
  sane-dates (plausibility window for UNGROUNDED dates only;
  `published > deadline` always fails), title-grounding (token overlap).
  Verdict `quarantine` ONLY on hard failures (extracted deadline not in the
  text, insane dates); `suspect` = soft failures (amounts/title), still matched.
- **`Opportunity.provenance/confidence/trust_verdict`** (contract,
  ARCHITECTURE.md §4): `provenance: "structured"|"llm"` declares how the fields
  were produced; LLM rows carry the report's confidence + verdict. All three are
  bookkeeping — EXCLUDED from `content_hash` (re-assessing never fakes an
  *amended*).
- **Quarantine exclusion upstream of Stage 1** (`core.exclude_quarantined`):
  quarantined rows stay in the DB (audit) but never reach the matcher; the
  prefilter itself stays pure and trust-agnostic (guardrail 4).
  `run_match(include_quarantined=True)` is the audit override.
- **Persistence**: the TrustReport lives BESIDE its cached extraction
  (`extractions.trust`, additive migration); a re-extraction resets it, legacy
  cache rows are backfilled with ONE page fetch (the LLM is never re-paid).
  `Store.trust_counts()` + `Store.list_by_trust_verdict()`.
- **Surfacing**: STATUS.md "Extraction trust" per-source table,
  `doctor --json` `trust_counts`, and a thin `bandiradar trust list` CLI
  (default: the quarantined set).

### Calibration (measured on the prod monitor DB, 139 cached extractions, 10 sources)
- First pass quarantined 13/139 (9.4%) — inspection showed ~all were CHECK
  false-positives, fixed by three retunes: FVG renders Scadenza in the English
  Java locale ("Thu Dec 31 23:59:00 CET 2026" — 11 items); recurring year-less
  deadlines ("entro il 31 gennaio di ciascun anno"); campania's curated set
  honestly lists 2023 bandi (old-but-grounded dates are archived data, not
  hallucinations — the sane-dates window now applies only to ungrounded dates).
- After retuning: **0% quarantine, 4.3% suspect (6/139, all amount-in-text —
  derived or attachment-only amounts, correctly soft), 95.7% ok, 0 unfetchable.**

## [0.11.0] — 2026-06-12 — Regional coverage wave 4 (final sweep)

### Added
- **`calabria`** — Calabria Europa (PR 2021-2027) bandi, as an LLM scraper. The
  institutional WP REST is permission-locked, but the PR portal's `bando` custom
  post type is OPEN over WP-REST (records carry only id/link/title) → the JSON
  listing seeds the crawl, the LLM extracts the rich detail pages.
- **`basilicata`** — the region's dedicated portalebandi, as an LLM scraper. The
  `avvisi-e-bandi` CPT is open over WP-REST; detail pages are structured
  (Destinatari, Importo, giorni alla scadenza). The portal publishes ALL avvisi
  (aste/concessioni too) — the LLM classification keeps tenders vs incentives
  honest.
- **`liguria`** — the portal's Joomla `publiccompetition` search, as an LLM
  scraper. Two server-side filters do the heavy lifting (tipologia "contributi" +
  stato "Attivi"); the crawl handles the session cookie + per-session CSRF token
  (two requests on one client). Detail pages are labelled (Data chiusura,
  Beneficiari, dotazione).
- **Documented skips, with every surface tried recorded in the coverage map:**
  - *Marche* — Radware bot-protection CAPTCHA across the whole domain family for
    datacenter callers (a JS challenge the relay cannot pass); open data holds
    only 2019/2020 gare archives; Svim unreachable.
  - *Umbria* — six surfaces, none viable: JS-only listings, empty Liferay shells,
    unreachable subdomains, dati.umbria.it does not resolve.
  - *Molise* — moliseineuropa lists stale 2014-2020-cycle avvisi; minimal volume.
  - *Valle d'Aosta* — the bandi section 403s datacenter callers; minimal volume.
  - *Bolzano* — service directories only, no bandi listing.
- Real recorded fixtures (live LLM extraction: 10 × 3) + 23 offline tests;
  cassettes for the JSON listings, the Liguria filtered-results page AND its
  quicksearch form (CSRF-token extraction).

Source adapters: 16 → **19** (9 key-less + 10 LLM scrapers); regional coverage
12 → **15 of 21 territories — every territory now has a verdict** (15 covered +
6 documented skips).

## [0.10.0] — 2026-06-12 — Regional coverage wave 3

### Added
- **`fvg`** — Regione FVG contributi-bearing bandi *in corso*, as an LLM scraper.
  The regional Socrata is retrospective-only, but the `regione.fvg.it` bandi
  module's search accepts the portal's own **"Bandi contenenti misure
  contributive"** filter (`onlyTagServizio=1`) as a GET, returning only the slice
  that matters (the unfiltered module mixes water-concession notices). CI note:
  the host drops GitHub-runner IPs but answers the EU-pinned relay (verified) —
  **first source ROUTED via the relay** (`BANDIRADAR_RELAY_HOSTS`); local fetches
  go direct, the per-host transport routing makes this transparent to the adapter.
- **`campania`** — Regione Campania curated open business bandi, as an LLM
  scraper. `fesr.regione.campania.it` blocks even the relay (500) and the main
  portal has no bandi listing; the viable surface is **Sviluppo Campania**
  (WP-REST auth-locked, `/feed` broken, but `/bandi-aperti/` is server-rendered).
  The crawl hooks the page's curated **media-image widget boxes** (~6 open
  business bandi: FRC II, Fondo Rotativo PMI, Garanzia Campania Bond, …) —
  deliberately skipping the closed-bandi nav submenu and the agency's own
  selection-notice archive. Honest scope documented. Pre-flight probes (direct +
  via relay) added BEFORE any routing decision; host added to the relay worker
  allowlist so routing is a one-line change if runners turn out blocked.
- **Abruzzo: documented SKIP.** Every surface — `regione.abruzzo.it`,
  `abruzzosviluppo.it`, the fesr/fse/abruzzoeuropa subdomains — blocks datacenter
  IPs INCLUDING the EU-pinned relay (500), and dati.gov.it carries zero Abruzzo
  bandi datasets. Recorded in the coverage map with the surfaces tried.
- Real recorded fixtures (live LLM extraction: 12 + 6 records) + offline tests;
  listing cassettes test the pure parsers (incl. the Campania widget hook
  against a cassette that CONTAINS both noise classes) and drift-to-broken.

Source adapters: 14 → **16** (9 key-less + 7 LLM scrapers); regional coverage
10 → **12**.

## [0.9.0] — 2026-06-12 — Regional coverage wave 2b

### Added
- **`puglia`** — Regione Puglia PR 2021-2027 avvisi, as an LLM scraper. Recon-first:
  the historic `sistema.puglia.it` is an Oracle-Portal service registry whose
  "Bandi Aperti" mixes 2010-era standing services with no scadenze, and its
  per-bando mini-sites are framesets with no content in the DOM (not viable);
  `por.regione.puglia.it` only holds the closed 2014-2020 cycle. The CURRENT
  portal `pr2127.regione.puglia.it` serves its avvisi via a Liferay resource URL
  returning a clean server-rendered `news-list-item` fragment with per-item
  **"Bando aperto"/"Bando chiuso" badges** (and `delta=30` one-call pagination) —
  the crawl keeps only items badged open, the LLM extracts each detail page.
- **`sardegna`** — Regione Sardegna agevolazioni from Sardegna Impresa (Drupal 10),
  as an LLM scraper. No jsonapi/RSS, but the `/it/agevolazioni` Views listing is
  server-rendered (full official titles, structured per-item scadenza datetime) and
  detail pages are labelled (Soggetti ammissibili, Data di scadenza, contributo).
- Real recorded fixtures (live LLM extraction) + offline tests for both; listing
  cassettes test the pure parsers (incl. Puglia's open-badge filter) and the
  drift-to-broken path.

Source adapters: 12 → **14** (9 key-less + 5 LLM scrapers); regional coverage
8 → **10**. Pre-flight probes updated to the real listing endpoints
(pr2127 elenco-avvisi, sardegnaimpresa /it/agevolazioni).

## [0.8.0] — 2026-06-12 — Regional coverage wave 2

### Added
- **`veneto`** — Regione del Veneto bandi from the SIU portal
  (`bandi.regione.veneto.it`), as an LLM scraper. Recon-first: the portal's
  listing is JS-driven and its internal JSON endpoint answers 200/empty to
  non-browser callers (cookies/headers replicated — bots are stonewalled), so the
  SERVER-RENDERED landing ("in scadenza" + latest atti) seeds the crawl and the
  LLM extracts each `Dettaglio` page. Honest scope: one visit surfaces the
  landing's ~10 atti; the daily monitor accumulates them over time.
- **`piemonte`** — Regione Piemonte bandi from the dedicated Drupal 10 portal
  (`bandi.regione.piemonte.it`), as an LLM scraper. Recon-first: no jsonapi, RSS
  only ~10 pre-informazione items, but the Views listing is server-rendered with
  an exposed stato filter — the crawl asks the server for **stato "Aperto"** only
  (`?field_stato_target_id=19`) and the LLM extracts each detail page.
- **`LlmScraperSource`** — reusable LLM-scraper base in `sources/llm_scraper.py`
  (per the module's charter): a subclass provides only its listing parse (a pure,
  cassette-tested function) + identity config; extraction, per-URL cache, the
  record→Opportunity mapper, fixture loading, and crawl-drift DETECTION
  (golden snapshot + `validate_refs`) are shared. An HTML listing parse is code,
  not a recipe, so drift is flagged for a human rather than LLM-healed (unlike
  `toscana`'s JSON-listing CrawlRecipe, which keeps the full healer).
- Real recorded fixtures (live LLM extraction) + offline tests for both sources;
  listing-HTML cassettes test the pure parsers and the drift path.

Source adapters: 10 → **12** (9 key-less + 3 LLM scrapers); regional coverage
6 → **8** (the two largest uncovered economies). Pre-flight probe for the
Piemonte bandi portal added; docs/coverage map/SVG updated.

## [0.7.0] — 2026-06-12 — Optional HTTP relay for CI-blocked sources

### Added
- **Optional HTTP relay** (`BANDIRADAR_RELAY_URL` / `BANDIRADAR_RELAY_TOKEN` /
  `BANDIRADAR_RELAY_HOSTS`) for sources whose endpoints drop datacenter IPs at the
  connection level (incentivi.gov.it from GitHub runners — the documented monitor
  gap). When all three env vars are set, requests to allowlisted hosts are
  transparently rewritten to `<relay>?u=<urlencoded-final-URL>` with an
  `X-Relay-Token` header. Generic and adapter-transparent: implemented at the
  TRANSPORT layer in `http.py` (`RelayTransport`, wired by `http.client()`;
  `stream_with_retry` rewrites up front), so the final URL — query already merged
  and encoded by httpx — is what gets relayed; no adapter was touched. With the env
  unset, behaviour is byte-for-byte unchanged (the repo stays keyless and fully
  functional; the relay worker is the operator's infrastructure; the token lives
  only in env/secrets, never the repo). The monitor workflow passes the secrets +
  `BANDIRADAR_RELAY_HOSTS=www.incentivi.gov.it`, and the pre-flight step probes
  incentivi both direct and via relay, logging both outcomes.

## [0.6.0] — 2026-06-12 — Regional coverage wave 1

### Added
- **`sicilia`** — Regione Siciliana / EuroInfoSicilia FESR/FSC incentives
  (`kind="incentive"`), key-less. The portal exposes bandi as STANDARD WordPress
  posts under the "Bandi e Avvisi" category (id 321), so the shared
  `WordPressBandiSource` base was extended MINIMALLY with an `extra_params` config
  (e.g. `{"categories": 321}`) to query the `/posts` endpoint with a category filter —
  no mapping fork. Real fixture + test.
- **`emilia_romagna`** — Regione Emilia-Romagna / Politiche territoriali incentives
  (`kind="incentive"`), key-less, via a NEW reusable `PloneBandoSource`
  (`sources/plone.py`). Many Italian PAs run Plone with the AGID `Bando` content type
  over plone.restapi (`@search?portal_type=Bando&fullobjects`), which carries a
  STRUCTURED `scadenza_bando` deadline (no text-parsing) plus `tipologia_bando`,
  `bando_state`, etc. Real fixture + test. (The old `fondieuropei.…` host now
  redirects to `politicheterritoriali.regione.emilia-romagna.it`.)
- **`trentino`** — Provincia Autonoma di Trento FEASR incentives
  (`kind="incentive"`), key-less, from a `dati.trentino.it` CKAN open-data CSV
  ("Calendario degli avvisi … Bandi FEASR") — dedicated open-data adapter (the
  Lombardia-style pattern); parses status/open-close dates/importo/link and carries
  currently-open bandi. Real fixture + test.
- Pre-flight reachability probe in the monitor workflow now also covers the three new
  endpoints.

Source adapters: 7 → **10** (9 key-less + 1 LLM scraper). See README "Sources" and
`docs/coverage-map.md`.

## [0.5.1] — 2026-06-11 — Monitor hardening

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
- **A long run can no longer eat STATUS/publish.** An LLM-scoring backlog (~2000 TED
  items) overran the 60-min job and was cancelled at the STATUS step, so `if: always`
  published a stale `STATUS.md` next to fresh data. The budget is now at the STEP: the
  watch loop has `timeout-minutes: 45` (job stays 60) and doctor + prune + STATUS +
  publish run `if: always()`, so they always get their slice. A truncated loop is
  surfaced HONESTLY: `watch --stats-out <p>.stats.json` writes a `{scored,deferred}`
  sidecar ONLY on completion, so `monitor_status.py` deduces truncation from missing
  sidecars and prints "⚠️ Run truncated: X/N profiles completed" — incomplete profiles
  show "incomplete", never a stale figure.
- **LLM spend cap per run (spike guard, default unchanged).** `BANDIRADAR_LLM_BUDGET`
  (`config.llm_budget()` → `relevance.LLMBudget`, threaded `run_watch`→`run_match`→
  `score_all`) caps NEW LLM scorings (cache MISSES) per run; over the cap, cache-miss
  items are DEFERRED (no Match this run — never heuristic-mixed inside an LLM run) and
  re-score in a later run as the cache fills. Cache hits and the heuristic backend are
  never capped; unset/`≤0` = unlimited. The monitor sets `1500`; `scored`/`deferred`
  surface via the sidecar into `STATUS.md`.
- **DB retention keeps the published branch under GitHub's 100 MB limit.**
  `storage.prune()` (+ a thin `bandiradar prune` command, no inline bash) drops
  `raw_docs` of opportunities closed > N days (default 90) and `runs` older than M days
  (default 30), then `VACUUM`s. It NEVER touches the score cache (the paid LLM value),
  watch markers, or crawl recipes/golden, and keeps the `opportunities` dedup ledger.
  Measured on the production DB: **52.3 MB → 39.1 MB (−13 MB)** on the first run.

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
