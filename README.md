# BandiRadar

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![tests](https://img.shields.io/badge/tests-pytest-brightgreen.svg)](tests/)

> Open-source engine that monitors Italian public funding opportunities
> (public tenders, grants, incentives), normalizes them into **one canonical
> model**, and ranks them against a company profile with a two-stage matcher.

## What it is

Italian public funding is scattered across dozens of fragmented sources.
BandiRadar pulls opportunities from those sources, maps each one into a single
canonical `Opportunity` model, and surfaces the few that matter for a given
company — with reasons and deadlines.

Matching is **two stages**:

1. **Deterministic prefilter** — a pure, explainable function (region/geo,
   value range, deadline, exclusions, and a relevance signal: the opportunity's
   CPV codes prefix-matched against the profile's `cpv_interests`, or a keyword
   overlap). Cuts thousands of rows to dozens. No LLM, no network.
2. **LLM relevance** — scores the survivors `0–100` with reasons, matched
   capabilities, eligibility flags, and risk notes. It ships with a
   **zero-secrets offline fallback** (a deterministic heuristic) so the whole
   thing runs in CI and in agent dev loops without any API key.

## 30-second quickstart (offline, no keys)

```bash
uv sync
uv run bandiradar match --profile data/profiles/mayai.yaml --sample
```

Real output on the bundled sample data:

```text
3 matching opportunities for 'MayAI':

#1  score 76  [closing_soon]  Fornitura di licenze software e servizi cloud GDPR-compliant
     issuer: Regione Lazio (Lazio)   deadline: 2026-06-08
     why: CPV prefix match (depth 2); capability overlap: cloud, digitalizzazione, gdpr, processi, software; within profile value range
     https://example.invalid/anac/notice/ocds-bandi-0002

#2  score 72  [open]  Servizi di analisi dati e machine learning per la PA centrale
     issuer: Ministero dell'Economia e delle Finanze (Lazio)   deadline: 2026-12-01
     why: CPV prefix match (depth 2); capability overlap: automazione, data, dati, learning, machine; within profile value range
     https://example.invalid/anac/notice/ocds-bandi-0004

#3  score 66  [open]  Servizi di sviluppo e manutenzione software gestionale comunale
     issuer: Comune di Roma Capitale (Lazio)   deadline: 2026-09-15
     why: CPV prefix match (depth 2); capability overlap: dati, software; within profile value range
     https://example.invalid/anac/notice/ocds-bandi-0001
```

Add `--json` for machine-readable output. The sample URLs are synthetic
(`example.invalid`) — see "Status" below.

## How it works

```
        ┌─────────┐   ┌───────────┐   ┌────────┐   ┌────────┐   ┌──────────┐
 sources│ INGEST  │──▶│ NORMALIZE │──▶│ STORE  │──▶│ MATCH  │──▶│ DELIVER  │
        └─────────┘   └───────────┘   └────────┘   └────────┘   └──────────┘
            fetch        raw→canonical   sqlite       2 stages     cli/mcp
                                         + dedupe                  (dashboard=pro)
```

A thin `core` service layer orchestrates the pipeline; the CLI and MCP server are
shells over it with no business logic. Storage is stdlib SQLite with **change
detection**: a changed `content_hash` bumps the version, marks the row
`amended`, and makes it re-notifiable (a tender *rettifica* should re-notify).
See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design.

## Sources

| Source | What it delivers | Live fetch |
|---|---|---|
| **`incentivi`** | incentivi.gov.it (MIMIT) — the national catalogue of **business incentives / grants** (`kind="incentive"`), national and regional. The grant side, and the source a digital SME profile actually matches. | ✅ Wired — the official IODL open-data export, no API key. |
| **`ted`** | TED — Tenders Electronic Daily, the EU's portal for **above-threshold, OPEN, biddable tenders** (includes large Italian public tenders). | ✅ Wired — anonymous, no API key. |
| **`anac`** | ANAC / PNCP open-contracting (OCDS) data — primarily **historical / award records**, a separate analytics track rather than open calls. | ⏳ Mapper + fixture done; live `fetch()` not wired. |

```bash
uv run bandiradar fetch --source incentivi --sample   # offline, bundled real capture
uv run bandiradar match --profile data/profiles/mayai.yaml --source incentivi --sample
uv run bandiradar match --profile data/profiles/mayai.yaml --source ted --sample
```

The `--sample` fixtures are **real captures** (`data/fixtures/{incentivi,ted}.json`).
`incentivi` exercises the canonical superset on the grant side — no CPV, a funding
range, and an eligibility text the matcher reads — and is where the MayAI dogfood
profile finds open national digital-services measures. TED carries above-threshold
contracts often far larger than a micro-SME's range, so a small profile matches
only the few that fit — which is exactly why incentive/national/regional sources
matter too.

> **Attribution (IODL):** incentivi.gov.it data is published by the Ministero
> delle Imprese e del Made in Italy under the
> [Italian Open Data License v2.0 (IODL 2.0)](https://www.dati.gov.it/iodl/2.0/),
> which requires attribution. Source: incentivi.gov.it. The live `incentivi`
> fetch hits the same open-data export endpoint the portal's own "Scarica
> dataset" button uses (there is no separate static file; the download is built
> client-side from that endpoint).

## Status (honest)

- ✅ **Runs today fully offline** on bundled sample data with **zero secrets** —
  both quickstarts above are real.
- ✅ **Two sources have live, key-less fetch:** `incentivi` (incentivi.gov.it
  open data) and `ted` (the EU search API). `--sample` keeps both offline against
  recorded real captures.
- ⏳ **The live ANAC/PNCP adapter is still pending.** Its mapping
  (`to_opportunities`) is implemented and tested against a recorded fixture, but
  the live `fetch()` is **not wired**: the open-data endpoint must be confirmed
  against current PNCP/ANAC docs first, so `fetch()` raises `NotImplementedError`
  until then. The ANAC fixture URLs are synthetic placeholders.
- ⚠️ **The offline scorer is a deterministic heuristic proxy**, not real semantic
  relevance. For real matching, set a provider and key:
  `BANDIRADAR_LLM_PROVIDER=anthropic` (or `openai`) plus the matching API key
  (see `.env.example`). With no key, BandiRadar transparently falls back to the
  heuristic.

## Use it from an AI agent (MCP)

BandiRadar ships a thin [MCP](https://modelcontextprotocol.io) server (FastMCP),
so you can drive it from Claude. Six tools:

`list_sources` · `fetch_opportunities` · `search_opportunities` ·
`score_opportunity` · `get_matches` · `get_profile`

```bash
uv run bandiradar mcp
```

Registration and an offline example session are in [`docs/MCP.md`](docs/MCP.md).

## Intelligence / benchmarks

A **separate** track (not the matcher) ingests ANAC *historical* OCDS data —
awarded public contracts — and computes compact benchmarks per **CPV-division ×
region**: award value distribution (median, p25/p75, min/max), volume,
seasonality (by year), and distinct-supplier counts.

```bash
uv run bandiradar benchmarks build --sample          # offline, bundled real capture
uv run bandiradar benchmarks show --cpv 45           # region falls back to national
```

Real output on the bundled sample:

```text
CPV division 45  [national]
  awards (count): 22   distinct suppliers: 21
  value EUR: median 470,768  p25 121,649  p75 1,594,879
  range: 68,117 – 11,369,083
  by year: 2022:22
```

**Honest data caveats:**
- The dataset is **retrospective** — *awarded* contracts (> €40k), not open calls.
- It has awards + suppliers but **no tenderers list**, so we **cannot** derive a
  "number of bidders". We derive value/volume/seasonality/supplier counts only.
- The release addresses carry city + postal code but **no region/NUTS**, so
  benchmarks are **national-only** for now (`region` stays `None`); the model and
  aggregation already support regional buckets for when a region-bearing source
  arrives.

> **Attribution (CC BY 4.0):** ANAC public-contracts data, via the
> [Open Contracting Data mirror](https://data.open-contracting.org/en/publication/117/)
> (CC BY 4.0). Live ingest streams the gzipped JSONL memory-safely.

### Enrichment: benchmarks in the matcher

The benchmarks are **optional matcher enrichment** (injected like the score
cache — the matcher works fine without them). Add `--with-benchmarks` to `match`
and each scored opportunity gets, for its CPV division, a historical-context
*reason* plus a *value-sanity* risk note when it declares an estimated value:

```bash
uv run bandiradar benchmarks build --sample
uv run bandiradar match --profile data/profiles/mayai.yaml --source ted --sample --with-benchmarks
```

```text
#1  score 44  [open]  Italia – Servizi di gestione dati – SERVIZIO DI GESTIONE ... ROCCA IMPERIALE (CS)
     why: CPV prefix match (depth 2); capability overlap: dati; eu scope; ANAC history (CPV 72, national): 8 awards, median EUR 104,326, p25-p75 EUR 71,619-183,410
     https://ted.europa.eu/en/notice/-/detail/376324-2026
```

Value-sanity triggers when the opportunity declares a value — e.g. on the ANAC
sample (`--source anac --with-benchmarks`):

```text
#  Servizi di analisi dati e machine learning per la PA centrale
   why: ...; ANAC history (CPV 72, national): 8 awards, median EUR 104,326, p25-p75 EUR 71,619-183,410
   risk: estimated value EUR 250,000 is above the historical p75 EUR 183,410 for this category
```

Enrichment is append-only on a **copy** of the cached match: the cache always
stores the bare match, so repeated runs never double-append. The
`search_opportunities` MCP tool takes the same `with_benchmarks` flag.

## Watch & export

`watch` is a monitor loop: it fetches, applies the storage change-detection, and
reports **only** matches whose opportunity is **new or amended** since the last
watch run (a per-profile marker is persisted; `--since` overrides it).

```bash
# 1st run: all current matches are "new"; a 2nd run reports nothing new
uv run bandiradar watch --profile data/profiles/mayai.yaml --source incentivi --sample
# write a feed instead of printing
uv run bandiradar watch --profile data/profiles/mayai.yaml --source incentivi --sample --rss ~/feed.xml
```

`export` is the full, non-delta dump of current matches (`--json` or `--rss PATH`).

**Scheduling is your cron** — this is open-core (single-user/local). For example:

```cron
0 8 * * *  cd /path/to/bandiradar && uv run bandiradar watch --profile mine.yaml --rss ~/feed.xml
```

Managed delivery (WhatsApp/email/alerts), scheduling SaaS, and multi-tenant
hosting live in `bandiradar-pro`.

## Open core vs Pro

Anything a single user can run locally is **open**. Anything *managed*,
*multi-client*, or *a delivery channel* lives in the private `bandiradar-pro`,
which depends on this package — never the reverse.

| | `bandiradar` (this repo, MIT) | `bandiradar-pro` (private) |
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

## Roadmap

**Shipped**
- Canonical model + `Source` framework + two-stage matcher (deterministic
  prefilter + LLM relevance with a zero-secrets offline fallback) + SQLite with
  change-detection + CLI + MCP server.
- Live sources: **TED** (EU open tenders) and **incentivi.gov.it** (national
  incentives), both key-less; ANAC OCDS mapper bundled.
- **Intelligence track:** ANAC historical benchmarks + optional matcher
  enrichment (`--with-benchmarks`).
- **`watch` monitor loop** (new/amended deltas) + **JSON/RSS export**.

**Upcoming**
- Live **ANAC/PNCP** fetch (confirm the open-data endpoint first).
- Embeddings-based prefilter; more community/regional source adapters (via the
  `Source` framework).
- `bandiradar-pro` (private): dashboard, WhatsApp/email delivery, scheduling
  SaaS, multi-tenant hosting.

## Add a source / Contributing

Every source is `fetch` + a pure `to_opportunities`, plus a recorded fixture and
a test — adding one is a new file, no core changes. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) and the `add-a-source` skill
(`skills/add-a-source/`) for the full copy-pasteable template; the playbook also
lives in `CLAUDE.md` ("How to add a new Source").

## License

MIT © MayAI — see [`LICENSE`](LICENSE).
