# BandiRadar

[![CI](https://github.com/mayai-it/bandiradar/actions/workflows/ci.yml/badge.svg)](https://github.com/mayai-it/bandiradar/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Open-source engine that monitors Italian public funding opportunities
> (public tenders, grants, incentives), normalizes them into **one canonical
> model**, and ranks them against a company profile with a two-stage matcher.

**Runs offline, zero secrets · 4 live key-less sources · optional LLM Stage-2 · MIT**

## Features

- **Two-stage matcher** — a deterministic prefilter + LLM relevance scoring, with
  a **zero-secrets offline heuristic fallback** (the LLM is optional).
- **4 live, key-less sources** — TED (EU), incentivi.gov.it (national), Regione
  Lombardia and Regione Lazio (regional); plus a bundled ANAC OCDS mapper.
- **ANAC historical-benchmark enrichment** — value/volume/seasonality context per
  CPV division, optionally attached to matches.
- **Document enrichment (PDF/OCR)** — optionally pull attachment PDFs into the
  matcher so it reads the real requirements, not just title + CPV.
- **`watch` monitor loop** (new/amended deltas) + **JSON/RSS export**.
- **CLI + MCP server** — drive it from a shell or from an AI agent.
- **Fully offline on `--sample`** — every demo and the whole test suite run with
  no network and no secrets.

## Table of contents

- [Quickstart](#quickstart)
- [Works across company types](#works-across-company-types)
- [How it works](#how-it-works)
- [Stage 2: LLM scoring](#stage-2-llm-scoring)
- [Sources](#sources)
- [Intelligence and benchmarks](#intelligence-and-benchmarks)
- [Document enrichment (PDF/OCR)](#document-enrichment-pdfocr)
- [Watch and export](#watch-and-export)
- [AI agents (MCP)](#ai-agents-mcp)
- [Status](#status)
- [Open core vs Pro](#open-core-vs-pro)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license) · [Data and licenses](#data-and-licenses)

## Quickstart

30 seconds, offline, no keys:

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

Add `--json` for machine-readable output. These ANAC sample URLs are synthetic
(`example.invalid`) — see [Status](#status).

## Works across company types

BandiRadar isn't tuned to one company — it runs any profile against every source.
`bandiradar batch` runs the bundled profile suite and compares results. Real
output on `--sample` (offline heuristic):

```text
PROFILE                          #  TOP MATCH (score)                      BY SOURCE
------------------------------------------------------------------------------------
Consulenza Strategica S.r.l.     8  Servizi di consulenza organizza… (72)  anac:2 incentivi:5 ted:1
Costruzioni Lombarde S.r.l.      2  LAVORI DI FORMAZIONE MANUTENZIO… (56)  lombardia:1 ted:1
Trattoria & Bottega S.r.l.       2  Manifestazione d'interesse per … (55)  incentivi:2
Manifattura Esempio S.r.l.       3  Fornitura di macchinari industr… (76)  anac:1 incentivi:2
MayAI                            5  Fornitura di licenze software e… (76)  anac:3 incentivi:1 ted:1
MedForniture Lombardia S.r.l.    3  FORNITURA DI DISPOSITIVI PER EN… (76)  lombardia:3
Studio Associato Commercialis…   4  Fornitura di licenze software e… (50)  anac:1 incentivi:2 ted:1
```

The suite spans distinct Italian SME segments — AI/software (MayAI),
manufacturing, medical-devices (Lombardy), accounting, construction,
hospitality/retail (keyword-driven, no CPV), and consultancy. Counts are real
matches on the tiny bundled sample; a segment can legitimately show few hits when
the sample doesn't cover it. Keyword/capability overlap ignores a curated list of
generic procurement filler (`lavori`, `servizi`, `fornitura`, `manutenzione`, …),
so matches reflect *sector-bearing* terms rather than boilerplate.

```bash
uv run bandiradar batch --sample              # human comparison table
uv run bandiradar batch --sample --json       # machine-readable
uv run bandiradar batch --sample --csv out.csv
```

With an LLM key the same table gets sharper scores and ranking — see
[Stage 2: LLM scoring](#stage-2-llm-scoring).

## How it works

```
        ┌─────────┐   ┌───────────┐   ┌────────┐   ┌────────┐   ┌──────────┐
 sources│ INGEST  │──▶│ NORMALIZE │──▶│ STORE  │──▶│ MATCH  │──▶│ DELIVER  │
        └─────────┘   └───────────┘   └────────┘   └────────┘   └──────────┘
            fetch        raw→canonical   sqlite       2 stages     cli/mcp
                                         + dedupe                  (dashboard=pro)
```

Italian public funding is scattered across dozens of fragmented sources.
BandiRadar pulls opportunities from those sources, maps each into a single
canonical `Opportunity` model, and surfaces the few that matter for a given
company — with reasons and deadlines. Matching is **two stages**:

1. **Deterministic prefilter** — a pure, explainable function (region/geo, value
   range, deadline, exclusions, and a relevance signal: the opportunity's CPV
   codes prefix-matched against the profile's `cpv_interests`, or a keyword
   overlap). Cuts thousands of rows to dozens. No LLM, no network.
2. **LLM relevance** — scores the survivors `0–100` with reasons, matched
   capabilities, eligibility flags, and risk notes. It ships with a **zero-secrets
   offline fallback** (a deterministic heuristic), so the whole thing runs in CI
   and in agent dev loops without any API key.

A thin `core` service layer orchestrates the pipeline; the CLI and MCP server are
shells over it with no business logic. Storage is stdlib SQLite with **change
detection**: a changed `content_hash` bumps the version, marks the row
`amended`, and makes it re-notifiable (a tender *rettifica* should re-notify).
See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design.

## Stage 2: LLM scoring

Stage 2 is **off by default** (zero secrets → deterministic offline heuristic).
To enable real LLM relevance scoring:

```bash
uv sync --extra anthropic        # or: --extra openai  (optional SDKs)
# in .env (gitignored):
#   BANDIRADAR_LLM_PROVIDER=anthropic        # or openai
#   ANTHROPIC_API_KEY=sk-ant-...             # or OPENAI_API_KEY=...
#   BANDIRADAR_LLM_MODEL=...                 # optional; defaults to a cheap Haiku-class model
```

`.env` is auto-loaded — no manual `export`. With **no key** (or no SDK), the
engine falls back to the heuristic, so CI and offline runs need nothing. When the
LLM path is active you'll see a one-time `scoring via anthropic:<model>` on stderr.

The LLM is more discriminating than the heuristic — same prefiltered set, sharper
scores/ranking (`bandiradar batch --sample`):

```text
                                  heuristic        LLM (anthropic, Haiku)
Costruzioni  (real construction)    56               92   ← genuine match promoted
Costruzioni  (IT doc-digitization)  (kept ~36)       15   ← cross-sector match demoted
MayAI        top match            software-licenses  ML/data tender (88)
Studio comm. software-licenses      50               25   ← weak fit penalized
MedForniture medical devices        76               92   ← strong sector fit held
```

## Sources

| Source | What it delivers | Live fetch |
|---|---|---|
| **`incentivi`** | incentivi.gov.it (MIMIT) — the national catalogue of **business incentives / grants** (`kind="incentive"`), national and regional. The grant side, and the source a digital SME profile actually matches. | ✅ Wired — the official IODL open-data export, no API key. |
| **`ted`** | TED — Tenders Electronic Daily, the EU's portal for **above-threshold, OPEN, biddable tenders** (includes large Italian public tenders). | ✅ Wired — anonymous, no API key. |
| **`lombardia`** | Regione Lombardia — **regional / sub-threshold** public tenders (`kind="tender"`), from the *Osservatorio Regionale* (Socrata SODA). Carries CPV, value, and province. | ✅ Wired — Socrata SODA, no API key. |
| **`lazio`** | Regione Lazio — **regional business incentives** (`kind="incentive"`), from the LazioInnova bandi portal (WordPress REST API). The source the MayAI dogfood profile matches. | ✅ Wired — WP REST, no API key. |
| **`anac`** | ANAC / PNCP open-contracting (OCDS) data — primarily **historical / award records**, a separate analytics track rather than open calls. | ⏳ Mapper + fixture done; live `fetch()` not wired. |

```bash
uv run bandiradar fetch --source incentivi --sample   # offline, bundled real capture
uv run bandiradar match --profile data/profiles/mayai.yaml --source incentivi --sample
uv run bandiradar match --profile data/profiles/mayai.yaml --source ted --sample
```

The `--sample` fixtures are **real captures** (`data/fixtures/*.json`).
`incentivi` exercises the canonical superset on the grant side — no CPV, a funding
range, and an eligibility text the matcher reads. TED carries above-threshold
contracts often far larger than a micro-SME's range, so a small profile matches
only the few that fit — which is why incentive/national/regional sources matter too.

A regional example (`data/profiles/medtech_lombardia.yaml`, a Lombardy
medical-devices distributor) matches open Lombardy tenders, while the Lazio-only
MayAI profile correctly drops them — regional filtering in action:

```bash
uv run bandiradar match --profile data/profiles/medtech_lombardia.yaml --source lombardia --sample
# -> 3 open medical-device tenders (region match, CPV 33*, within value range)
uv run bandiradar match --profile data/profiles/mayai.yaml --source lombardia --sample
# -> No matching opportunities (Lazio profile, Lombardy bandi dropped on region)
```

And the dogfood closes the loop — MayAI **is** a Lazio company, and `lazio`
(LazioInnova) is where it finally matches its own region:

```bash
uv run bandiradar match --profile data/profiles/mayai.yaml --source lazio --sample
# -> Voucher Digitalizzazione PMI 2025 (52), Donne e Impresa 2026 (42, closing soon)
#    region match: Lazio; overlap: digitalizzazione, software, cloud, dati
```

Source data licensing is consolidated under [Data and licenses](#data-and-licenses).

### Regional coverage

WordPress-based regional portals (like LazioInnova) are **config-only** to add:
`WordPressBandiSource` (`sources/wordpress.py`) captures the whole WP-REST pattern
(fetch, pagination, scadenza parsing, taxonomy→keywords), so a new such region is
a config entry + a fixture + a test, not new code.

Honestly, though, that clean pattern is **rare** — most Italian regional agency
portals are bespoke sites with no public open-bandi API, so each new region
usually needs its own adapter (CKAN/Socrata like `lombardia`, or HTML scraping)
rather than a one-line config. We don't ship half-working adapters: a portal
that's unreachable, retrospective-only, or API-less is skipped, not faked. The
per-region status (what's been checked, where coverage is needed) lives in
[`docs/regions.md`](docs/regions.md) — **regional contributions are very welcome.**

## Intelligence and benchmarks

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
`search_opportunities` MCP tool takes the same `with_benchmarks` flag. ANAC data
licensing is under [Data and licenses](#data-and-licenses).

## Document enrichment (PDF/OCR)

Most of a tender's real requirements live in attachment PDFs (the
*disciplinare*/*bando*), not in the title or CPV. With `--with-documents`, the
matcher fetches an opportunity's `document_urls`, extracts the text, and folds it
into **every** matching input — the prefilter keyword gate, the offline
heuristic's overlap, and the LLM prompt — so requirements that exist only in the
attachments can still drive (or sink) a match. Extracted text is cached per URL
(SQLite), so PDFs aren't re-downloaded.

```bash
uv run bandiradar match --profile mine.yaml --source ted --sample --with-documents
```

- **Optional and injected** — like the score/benchmark caches; off by default.
  The default install only needs `pypdf`.
- **OCR for scanned PDFs** is the optional `ocr` extra:
  `uv sync --extra ocr` plus the system binaries `tesseract` and `poppler`. When
  absent, OCR is skipped cleanly (text-based PDFs still work). Enrichment never
  raises into the matcher — a failed fetch/parse degrades to no added text.
- **Honest source coverage:** only `ted` currently carries a per-notice document
  link (the notice PDF). `lombardia`, `incentivi`, and `anac` expose no per-document
  attachment URL in their data, so `document_urls` is empty for them (no faking) —
  until those links are wired, `--with-documents` is a no-op there.

> `--with-documents` fetches PDFs over the network, so (unlike the default
> `--sample` flow) it is **not** offline.

## Watch and export

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

## AI agents (MCP)

BandiRadar ships a thin [MCP](https://modelcontextprotocol.io) server (FastMCP),
so you can drive it from Claude. Six tools:

`list_sources` · `fetch_opportunities` · `search_opportunities` ·
`score_opportunity` · `get_matches` · `get_profile`

```bash
uv run bandiradar mcp
```

Registration and an offline example session are in [`docs/MCP.md`](docs/MCP.md).

## Status

- ✅ **Offline, zero-secret** — every demo above and the whole test suite run with
  no network and no API key.
- ✅ **4 live key-less sources** — `incentivi`, `ted`, `lombardia`, `lazio`. `--sample`
  keeps them offline against recorded real captures.
- ✅ **Stage-2 LLM scoring is wired and working** (optional); with no key it
  transparently uses the offline heuristic — a proxy, not real semantic relevance.
- ⏳ **Live ANAC/PNCP fetch is pending** — the mapper + fixture are done and
  tested, but `fetch()` raises `NotImplementedError` until the open-data endpoint
  is confirmed against current docs. The ANAC sample URLs are synthetic.

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
- Live sources: **TED** (EU open tenders), **incentivi.gov.it** (national
  incentives), **Regione Lombardia** (CKAN/Socrata tenders) and **Regione Lazio**
  (LazioInnova incentives), all key-less; ANAC OCDS mapper bundled.
- **Intelligence track:** ANAC historical benchmarks + optional matcher
  enrichment (`--with-benchmarks`).
- **`watch` monitor loop** (new/amended deltas) + **JSON/RSS export**.

**Upcoming**
- Live **ANAC/PNCP** fetch (confirm the open-data endpoint first).
- Embeddings-based prefilter; more community/regional source adapters (via the
  `Source` framework — Lombardy is the first; other regions welcome).
- `bandiradar-pro` (private): dashboard, WhatsApp/email delivery, scheduling
  SaaS, multi-tenant hosting.

## Contributing

Every source is `fetch` + a pure `to_opportunities`, plus a recorded fixture and
a test — adding one is a new file, no core changes. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) and the `add-a-source` skill
(`skills/add-a-source/`) for the full copy-pasteable template; the playbook also
lives in `CLAUDE.md` ("How to add a new Source").

## License

MIT © MayAI — see [`LICENSE`](LICENSE).

## Data and licenses

BandiRadar consumes public open data; each source keeps its own licence, which
its operator requires you to honor:

- **incentivi.gov.it (IODL 2.0)** — published by the Ministero delle Imprese e del
  Made in Italy under the
  [Italian Open Data License v2.0](https://www.dati.gov.it/iodl/2.0/) (attribution
  required). The live `incentivi` fetch hits the same open-data export endpoint the
  portal's own "Scarica dataset" button uses (no separate static file; the download
  is built client-side from that endpoint).
- **Regione Lombardia (CC0 1.0)** — dataset `k6cb-4hbm` (*Bandi di gara —
  Osservatorio Regionale*), via the dati.lombardia.it Socrata SODA API.
- **Regione Lazio / LazioInnova** — bandi published by LazioInnova (the regional
  development agency) and read via its WordPress REST API
  (`lazioinnova.it/wp-json`). Source: LazioInnova / Regione Lazio; attribute the
  source when reusing.
- **ANAC public contracts (CC BY 4.0)** — via the
  [Open Contracting Data mirror](https://data.open-contracting.org/en/publication/117/);
  the intelligence track streams the gzipped JSONL memory-safely.
