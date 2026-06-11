# BandiRadar — Architecture (v1)

> Open-source engine that monitors Italian public funding opportunities (public
> tenders, grants, incentives), normalizes them into **one canonical model**, and
> ranks them against a company profile with an LLM.
>
> This document is the single source of truth for the design. It is written to be
> read by both humans and coding agents. Keep it in sync with `CLAUDE.md`.

---

## 1. Mission & strategy

**What it does.** Pulls opportunities from many fragmented Italian sources, maps
them into a single `Opportunity` model, and uses a two-stage matcher (cheap
deterministic prefilter + LLM relevance scoring) to surface the few that matter
for a given company, with reasons and deadlines.

**Why it exists (for MayAI).** A flagship open-source repo that builds technical
authority with the Italian SME/commercialista audience and acts as a funnel to a
paid, managed product. The repo is the showcase; the managed experience is the
business.

**The real moat is NOT the ANAC adapter** (anyone can write one). It is:
1. a clean **canonical model** that heterogeneous sources map into, and
2. **matching quality**.

So the architecture optimizes for *"adding a new source is cheap"*, not for ANAC
specifically.

---

## 2. Open-core boundary (two repos)

| | `bandiradar` (PUBLIC, MIT) | `bandiradar-pro` (PRIVATE) |
|---|---|---|
| Engine (ingest/normalize/match) | ✅ | imports it |
| Source framework (`Source` interface + registry) | ✅ | |
| Reference adapters (ANAC, incentivi.gov.it) | ✅ | |
| Two-stage matcher (incl. offline fallback) | ✅ | |
| CLI + MCP server | ✅ | |
| Dashboard (web UI) | | ✅ |
| Premium / regional source adapters | | ✅ |
| Delivery channels (WhatsApp, email, alerts) | | ✅ |
| Multi-tenant, managed hosting, scheduling SaaS | | ✅ |

**Boundary rule of thumb:** anything a single user can run locally for themselves
is **open**. Anything that is *managed*, *multi-client*, or *a delivery channel*
is **pro**. `bandiradar-pro` depends on `bandiradar` as a normal package — never
the reverse.

Why two repos and not one with "private" folders: clean seams, separate
`CLAUDE.md` per repo, and no risk that a stray prompt in Claude Code mixes paid
code into the public repo.

---

## 3. Pipeline

```
        ┌─────────┐   ┌───────────┐   ┌────────┐   ┌────────┐   ┌──────────┐
 sources│ INGEST  │──▶│ NORMALIZE │──▶│ STORE  │──▶│ MATCH  │──▶│ DELIVER  │
        └─────────┘   └───────────┘   └────────┘   └────────┘   └──────────┘
            fetch        raw→canonical   sqlite       2 stages     cli/mcp
                                         + dedupe                  (dashboard=pro)
```

A `core` service layer orchestrates these. **Interfaces (CLI / MCP / dashboard)
are thin shells** over `core` and contain *no business logic*. This is what makes
the codebase legible to agents and safe to edit file-by-file.

---

## 4. Canonical model (the contract — do not break casually)

One superset model covers tenders **and** grants/incentives, so adding the grant
sources later needs no refactor. Tender fields are aligned to **OCDS** (Open
Contracting Data Standard) where applicable.

```python
class Opportunity(BaseModel):
    id: str                      # stable, source-prefixed: "anac:<ocid>"
    source: str                  # source id, e.g. "anac"
    source_url: str
    kind: Literal["tender", "grant", "incentive"]

    title: str
    summary: str | None
    issuer_name: str | None      # buyer / granting body
    issuer_region: str | None

    cpv: list[str] = []          # tender procurement codes
    ateco_hints: list[str] = []  # mapped/declared business codes
    keywords: list[str] = []

    value_amount: float | None
    value_currency: str = "EUR"
    value_min: float | None
    value_max: float | None

    geo_scope: Literal["national", "regional", "eu", "local"]
    region: str | None

    published_at: datetime | None
    deadline: datetime | None
    updated_at: datetime | None
    status: Literal["open", "closing_soon", "closed"]  # lifecycle, derived on read

    eligibility_text: str | None # free text fed to the matcher
    document_urls: list[str] = []  # attachment/doc links (disciplinare/bando PDFs)
    document_text: str | None    # text extracted from those docs (optional enrichment)
    raw_ref: str                 # pointer to stored RawDoc
    content_hash: str            # for change detection
    version: int = 1
```

`RawDoc` = the untouched payload from a source (for audit + re-mapping).
`Profile` = the company we match against (see §7).

**Contract invariants (enforced in `models.py`):**
- **`content_hash`** is a deterministic SHA-256 over the *semantically meaningful*
  fields only: `title`, `summary`, `issuer_name`, `issuer_region`,
  `value_*` (amount/currency/min/max), `deadline`, `eligibility_text`, `kind`,
  `cpv`, `region`, `geo_scope`. It deliberately **excludes** `version`,
  `updated_at`, `keywords`, and `ateco_hints` (bookkeeping / derived hints), and
  also `document_urls` / `document_text` (optional downstream enrichment — see §6),
  so change detection (§8) fires on substance, not on re-fetch noise or enrichment.
- **Datetimes are tz-aware UTC.** All datetime fields (`Opportunity.published_at`
  / `deadline` / `updated_at`, `RawDoc.fetched_at`) coerce naive inputs to UTC,
  so downstream comparisons (e.g. the prefilter) never hit naive-vs-aware errors.
- **`Opportunity` and `Profile` are `extra="forbid"`**: a mis-mapped adapter or
  profile field fails loudly instead of being silently dropped.
- **`status` is purely lifecycle, derived on READ.** `default_status(deadline,
  now)` is the single source of truth; storage recomputes `status` on every read
  (§8) so a stored item never shows "open" past its deadline. The "this notice
  changed" signal is **separate** — it lives in `version` + `updated_at` (bumped on
  a content_hash change) and is surfaced by `list_new(since)` / the watch delta.
  (Pre-0.2.0 overloaded `status` with a sticky `"amended"`; that's removed — reads
  tolerate the old value and recompute it.)

---

## 5. Source framework (the extension point)

Every source is `fetch` + `map`. It must ship a recorded **fixture** so it is
testable offline with no network and no secrets.

```python
class Source(Protocol):
    id: str
    kind: Literal["tender", "grant", "incentive"]

    def fetch(self, since: datetime | None = None) -> Iterable[RawDoc]: ...
    def to_opportunities(self, raw: RawDoc) -> list[Opportunity]: ...
```

Sources self-register (entry points / a registry dict) so pro and community
adapters plug in **without touching core**. Adding a source = new file +
fixture + test. This is the path to "integrate everything later": the long tail
of regional bandi can be crowdsourced by the community against this interface.

**v1 reference adapters**
- `anac` — ANAC / PNCP open contracting data (OCDS). Real, structured, free.
  Wired to the Open Contracting mirror (key-less), streamed line by line via the
  shared `bandiradar/ocp.py` reader and **capped** (500 releases/run). The data is
  **retrospective** — *awarded* contracts, not open calls — so it surfaces
  mostly-closed opportunities (the matcher drops them); its real value is the
  historical/benchmark track. The OCDS address carries no region/NUTS, so
  `region=None` and `geo_scope="national"`. `to_opportunities` stays pure; the
  network/streaming lives in `fetch()`.
- `anac_pvl` — ANAC *Pubblicità a Valore Legale* (`pubblicitalegale.anticorruzione.it`),
  the **live feed of OPEN tenders** (`kind="tender"`) that `anac` (retrospective OCDS)
  and `ted` (EU above-threshold) miss. Public JSON API, no credentials. The robust
  filter is **`dataScadenza` in the future** (an original `tipo=="avviso"`, active,
  not obscured) — NOT `codiceScheda` (recorded only for reference). `fetch()`
  pre-filters to open gare so the limit budget isn't spent on the ~70%
  esiti/rettifiche; rettifiche are caught later by `content_hash` change-detection,
  not an N+1 `/cronologia` call. Mapper is pure. Region is resolved structured-first:
  `luogo_nuts` (province) → `luogo_istat` (comune, ISTAT table) → a conservative
  "Comune di X" buyer parse → national. PVL's `cpv` is the Italian **label**; it is
  resolved to the official 8-digit code via `bandiradar.cpv` (packaged EU CPV 2008
  vocabulary; often coarse division-level), with the label kept as keyword text.
- `incentivi` — incentivi.gov.it (national business incentives). Phase 1.

Regional adapters = phase 2, community-contributed via the `add-a-source` skill.

**Reusable WordPress base.** Many regional agencies run WordPress and expose
their bandi as a custom post type over the WP REST API. `WordPressBandiSource`
(`sources/wordpress.py`) captures that whole pattern — fetch/pagination, HTML
stripping, scadenza parsing, taxonomy→keywords — so such a region is a **config
entry**, not a new module: `WordPressBandiSource(id, region, data_url,
issuer_name, kind, keyword_taxonomies)` + a fixture + a test. `sources/lazio.py`
(LazioInnova) is the reference config. In practice this clean pattern is rare —
most regional portals are bespoke and need a dedicated adapter (CKAN/Socrata like
`lombardia`, or HTML scraping); see `docs/regions.md` for the coverage map.

**LLM HTML scraper + self-healing crawl.** When a regional portal has *no* clean
data API and only HTML pages (e.g. `toscana`), an LLM reads each detail page and
extracts the canonical fields (`sources/llm_scraper.py`), so a new region configures
only its *crawl*, not a bespoke parser. The extraction (an LLM call) lives in
`fetch()`; `to_opportunities` stays pure over the extracted fields. The fragile
dependency of such a scraper is the **crawl** (the listing it walks to find detail
pages), not the extraction — the LLM already adapts to HTML changes. So the crawl is
modelled as a `CrawlRecipe`: **DATA, not code** (`crawl.py`, a stdlib, dependency-free
spine; re-exported by `llm_scraper`). `validate_refs` detects DRIFT, and on drift the
LLM healer (`sources/heal.py`) re-derives a candidate recipe — still DATA, never a
code change. **Golden guard:** a candidate is ADOPTED only if it reproduces the
recorded *golden* refs EXACTLY (`crawl.recipe_reproduces_golden`), via a single
guarded `recipe_store.adopt()`; otherwise it is flagged for human review, never
auto-applied. This keeps a self-modifying system honest — the LLM proposes, but a
deterministic, un-bypassable socket decides. Recipes + golden persist in SQLite
(`crawl_recipes`, `crawl_golden`); per-source overrides + golden config (auditable
`{recipe, adopted_at, reason, validated_by}`) live in `recipe_store.py`.

**CPV label resolver.** Some sources expose the CPV as the Italian *label* rather
than the numeric code (notably `anac_pvl`), which would blind the prefilter's
CPV-prefix gate. `bandiradar.cpv` resolves a label → official 8-digit CPV code via an
exact normalized match against the packaged EU CPV 2008 vocabulary
(`data/cpv_it.json`); pure + offline, often coarse (division/group level), with the
label kept as keyword text when it doesn't resolve.

---

## 6. Matching engine (two stages)

**Stage 1 — deterministic prefilter (no LLM, pure function, fully tested).**
Ordered gates: `deadline > now`, instrument type (`Profile.seeks`: grant vs
tender), region/geo, value range, exclusion terms, and a relevance signal: the
opportunity's CPV codes prefix-matched against the profile's `cpv_interests`, OR a
keyword overlap. (There is no ATECO→CPV mapping; ATECO lives on the profile as
metadata and is not used as a prefilter gate.) Cuts thousands of rows to dozens.
Cheap and explainable.

*Optional hybrid signal (off by default):* when an `Embedder` is injected
(`run_match(embedder=…)`, the `embeddings` extra), the relevance gate also passes on
`cosine(profile, opportunity) ≥ threshold` — a semantic recall lever for items that
share no exact CPV/keyword. Vectors cache by `content_hash`. Whether it nets
positive is measured by `eval --embeddings`; the default path stays pure (no model,
no network). See `matching/embeddings.py`.

**Stage 2 — LLM relevance.**
Input: `(profile, opportunity minimal text)`. Structured output:
```
score: int            # 0-100
reasons: list[str]
matched_capabilities: list[str]
eligibility_flags: list[str]
risk_notes: list[str]
```
The persisted `Match` also carries `opportunity_id`, `profile_version`, and
`opportunity_hash` (the Opportunity.content_hash at scoring time) — the two
latter form the cache key.

Design rules:
- **Provider-agnostic client.** Default to a strong model (Anthropic/OpenAI) now;
  swapping to an EU/GDPR-friendly model (Mistral, local) is a config/env change,
  not a refactor.
- **Cache** keyed by `(profile_version, opportunity_hash)` → `Match`. Because the
  key includes the opportunity's `content_hash`, an **amended** opportunity (new
  hash) misses the cache and is re-scored automatically, while unchanged re-runs
  cost nothing.
- **Offline heuristic fallback** (keyword/embedding-lite) when no API key is set,
  so the repo runs in CI and in agent dev loops with **zero secrets**.
- **Privacy:** send the minimal opportunity text + a compact profile summary,
  never raw dumps. (Tender data is public; the profile may not be.)
- **Optional document enrichment.** When enabled, attachment PDFs
  (`Opportunity.document_urls`) are fetched and their extracted text
  (`document_text`) is folded into every matcher input — the Stage-1 keyword gate,
  the heuristic overlap, and the LLM brief — so requirements that live only in the
  *disciplinare* still drive matching. Injected/cached like the score cache;
  `document_text` is excluded from `content_hash` (§4) so enrichment never fakes an
  *amended*. OCR (scanned PDFs) is an optional extra.

---

## 7. Company profile (YAML, one per client)

```yaml
name: "MayAI"
language: it
ateco: ["62.01", "62.02", "63.11"]
cpv_interests: ["72000000", "48000000"]   # IT services / software
keywords: []                              # optional free-text match terms
regions: ["Lazio"]                        # only sub-national regions; national/eu
                                          # scope is handled by geo_scope, not here
value_range: { min: 5000, max: 250000 }
capabilities: >
  AI consulting and vertical software for Italian SMEs; data science,
  ML, process automation, GDPR-compliant EU cloud.
exclusions: ["construction", "catering"]
seeks: ["grant"]                          # instrument classes pursued; default BOTH
```

`keywords` is an optional list of free-text terms; the Stage-1 prefilter treats a
case-insensitive substring hit in an opportunity's title/summary as a relevance
signal (alongside CPV matching).

`seeks` (`list["grant"|"tender"]`, **default `["grant","tender"]`**) declares which
instrument classes the company pursues: GRANTS/incentives are applied for; public
TENDERS are bid on (selling to the PA). It is a HARD Stage-1 gate — an opportunity
whose class is not in `seeks` is dropped (tenders map to `"tender"`, grants and
incentives to `"grant"`; see `matching.prefilter.seek_class`). The default seeks
both, so existing/unset profiles are unaffected. Example: an AI studio or accounting
firm sets `["grant"]` (no procurement bidding); a construction firm or medical-device
distributor keeps `["grant","tender"]`.

**Dogfood example profile = MayAI itself** (an AI studio chasing
digitalization/innovation incentives) + a generic manufacturing PMI fixture.
Self-referential demo + free marketing.

---

## 8. Storage

SQLite (stdlib, zero-config, agent-friendly). Tables: `opportunities`,
`raw_docs`, `matches`, `runs` (plus `crawl_recipes` + `crawl_golden` for the
self-healing crawl, see §5). Dedupe + **change detection** via `content_hash`:
a changed hash bumps `version` and stamps `updated_at`, making the row eligible to
be re-surfaced (a tender *rettifica* should re-notify) via `list_new(since)` and
the watch delta. The change signal is kept **out** of `status`, which is recomputed
from `deadline` + now on every read so it is always current. Each fetch also
records one `runs` row per source (status, structured `error_kind`, counts,
duration). Schema upgrades are additive via a PRAGMA-introspecting migration, so
old DBs open cleanly. Without change detection it is a query, not a *monitor*.

---

## 9. Interfaces (thin)

- **CLI (Typer):** `profile`, `sources`, `fetch`, `match`, `watch`, `mcp`.
  `--json` on every command. Defaults to offline sample mode.
- **MCP server (FastMCP):** tools `list_sources`, `fetch_opportunities`,
  `search_opportunities`, `score_opportunity`, `get_matches`, `get_profile`.
  Dogfood: drive BandiRadar from Claude itself.
- **Dashboard:** lives in `bandiradar-pro`, not here.

---

## 10. Phasing (architecture is complete now; delivery is staged)

- **Phase 0 — v1 spine (this build):** canonical model + Source framework + ANAC
  adapter (offline fixture + live capped OCDS streaming) + two-stage matcher (with
  offline fallback) + SQLite + CLI + MCP + tests + README. Ships as a credible
  public repo that runs **end-to-end on sample data with zero secrets**.
- **Phase 1:** `incentivi` adapter, `watch`/scheduling, JSON/RSS export,
  embeddings-based prefilter.
- **Phase 2:** regional adapters (community via the framework) + `bandiradar-pro`
  (dashboard, WhatsApp/email delivery, multi-tenant, hosting).

---

## 11. Tech stack

Python 3.12 · pydantic v2 · httpx · Typer · FastMCP · SQLite (stdlib) ·
ruff · pytest · uv. No heavy framework in core.

---

## 12. AI-native conventions (summary — full version in `CLAUDE.md`)

- **Vertical-slice first.** Build one end-to-end thread, green at each step,
  before widening.
- **Pure + fixture-tested** sources and Stage-1 matcher → agents self-verify.
- **Offline-runnable always** → no secrets needed to build/test.
- **Skills:** `bandiradar` (drive the CLI) and `add-a-source` (the adapter
  pattern) ship in the repo so agents don't need re-explaining.
