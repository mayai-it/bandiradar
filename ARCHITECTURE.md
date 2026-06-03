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
    status: Literal["open", "closing_soon", "closed", "amended"]

    eligibility_text: str | None # free text fed to the matcher
    raw_ref: str                 # pointer to stored RawDoc
    content_hash: str            # for change detection
    version: int = 1
```

`RawDoc` = the untouched payload from a source (for audit + re-mapping).
`Profile` = the company we match against (see §7).

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
- `anac` — ANAC / PNCP open contracting data (OCDS). The backbone: real,
  structured, free. *Confirm the current PNCP/ANAC open-data endpoint against
  live docs before wiring the live fetch; ship the sample fixture first.*
- `incentivi` — incentivi.gov.it (national business incentives). Phase 1.

Regional adapters = phase 2, community-contributed via the `add-a-source` skill.

---

## 6. Matching engine (two stages)

**Stage 1 — deterministic prefilter (no LLM, pure function, fully tested).**
Filters on region/geo, `cpv ∩ ateco`, value range, `deadline > now`, keyword
overlap. Cuts thousands of rows to dozens. Cheap and explainable.

**Stage 2 — LLM relevance.**
Input: `(profile, opportunity minimal text)`. Structured output:
```
score: int            # 0-100
reasons: list[str]
matched_capabilities: list[str]
eligibility_flags: list[str]
risk_notes: list[str]
```
Design rules:
- **Provider-agnostic client.** Default to a strong model (Anthropic/OpenAI) now;
  swapping to an EU/GDPR-friendly model (Mistral, local) is a config/env change,
  not a refactor.
- **Cache** by `hash(profile.version + opportunity.content_hash)` → score.
  Re-runs cost nothing.
- **Offline heuristic fallback** (keyword/embedding-lite) when no API key is set,
  so the repo runs in CI and in agent dev loops with **zero secrets**.
- **Privacy:** send the minimal opportunity text + a compact profile summary,
  never raw dumps. (Tender data is public; the profile may not be.)

---

## 7. Company profile (YAML, one per client)

```yaml
name: "MayAI"
language: it
ateco: ["62.01", "62.02", "63.11"]
cpv_interests: ["72000000", "48000000"]   # IT services / software
regions: ["Lazio", "national"]
value_range: { min: 5000, max: 250000 }
capabilities: >
  AI consulting and vertical software for Italian SMEs; data science,
  ML, process automation, GDPR-compliant EU cloud.
exclusions: ["construction", "catering"]
```

**Dogfood example profile = MayAI itself** (an AI studio chasing
digitalization/innovation incentives) + a generic manufacturing PMI fixture.
Self-referential demo + free marketing.

---

## 8. Storage

SQLite (stdlib, zero-config, agent-friendly). Tables: `opportunities`,
`raw_docs`, `matches`, `runs`. Dedupe + **change detection** via `content_hash`:
a changed hash bumps `version`, sets status `amended`, and makes the row eligible
to be re-surfaced (a tender *rettifica* should re-notify). Without this it is a
query, not a *monitor*.

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
  adapter (sample + live) + two-stage matcher (with offline fallback) + SQLite +
  CLI + MCP + tests + README. Ships as a credible public repo that runs
  **end-to-end on sample data with zero secrets**.
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
