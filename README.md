# BandiRadar

[![PyPI](https://img.shields.io/pypi/v/bandiradar.svg)](https://pypi.org/project/bandiradar/)
[![CI](https://github.com/mayai-it/bandiradar/actions/workflows/ci.yml/badge.svg)](https://github.com/mayai-it/bandiradar/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Open-source engine that monitors Italian public funding opportunities
> (public tenders, grants, incentives), normalizes them into **one canonical
> model**, and ranks them against a company profile with a two-stage matcher.
> One normalized feed of **OPEN Italian tenders** (incl. sub-threshold gare) **+
> incentives**, behind a crawl that **repairs itself** when a portal drifts.

**Runs offline, zero secrets · 6 live key-less sources + 1 LLM-assisted scraper · includes live OPEN Italian tenders (incl. sub-threshold) · optional LLM Stage-2 · MIT**

## Coverage

> **[Coverage map](docs/coverage-map.md)** — an honest map of where Italian public
> funding is published and what BandiRadar covers: open feeds vs gated, with the
> honest gap.

[![Italian public funding — data coverage map](docs/coverage-map.svg)](docs/coverage-map.md)

## Features

- **Two-stage matcher** — a deterministic prefilter + LLM relevance scoring, with
  a **zero-secrets offline heuristic fallback** (the LLM is optional).
- **6 live, key-less sources** — TED (EU), incentivi.gov.it (national), `anac_pvl`
  (national open tenders), Regione Lombardia and Regione Lazio (regional); plus ANAC
  OCDS as a key-less **historical / awarded-contracts** feed (analysis, not open
  calls). Regione Toscana is an **LLM-assisted scraper** (live fetch needs an LLM
  key; `--sample` replays a recorded extraction offline).
- **Live OPEN Italian tenders** (`anac_pvl`) — the national *Pubblicità a Valore
  Legale* feed of open, biddable gare, **incl. sub-threshold** ones TED never lists,
  **no credentials** — the biddable feed the other sources lack.
- **Self-healing crawl** — when a scraper's listing drifts, an LLM **re-derives the
  crawl recipe** (data, not code); it's adopted **only if it exactly reproduces the
  last-good results**, otherwise human-flagged — never silently. Demonstrated on
  Toscana.
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
- [Self-healing crawl](#self-healing-crawl)
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
pip install bandiradar
bandiradar match --profile mayai --sample
```

Or from a source checkout:

```bash
uv sync
uv run bandiradar match --profile mayai --sample
```

Real output on the bundled sample data:

```text
4 matching opportunities for 'MayAI':

#1  score 55  [open]  Manifestazione d'interesse per l'accesso ai servizi per la digitalizzazione forniti da SoE AP EDIH
     issuer: Ministero delle Imprese e del Made in Italy (Campania)   deadline: 2026-06-30
     why: capability overlap: artificiale, digitalizzazione, intelligenza; within profile value range; national scope
     https://www.medisdih.it/wp/

#2  score 52  [open]  Voucher Digitalizzazione PMI 2025
     issuer: LazioInnova (Lazio)   deadline: —
     why: capability overlap: cloud, conforme, dati, digitalizzazione, machine; region match: Lazio
     https://www.lazioinnova.it/bandi/voucher-digitalizzazione-pmi-2025/

#3  score 44  [open]  Italia – Servizi di gestione dati – SERVIZIO DI GESTIONE … COMUNE DI ROCCA IMPERIALE (CS)
     issuer: CENTRALE UNICA DI COMMITTENZA … CASSANO ALL'IONIO E TREBISACCE (—)   deadline: —
     why: CPV prefix match (depth 2); capability overlap: dati; eu scope
     https://ted.europa.eu/en/notice/-/detail/376324-2026

#4  score 42  [closing_soon]  Donne e Impresa 2026
     issuer: LazioInnova (Lazio)   deadline: 2026-06-10
     why: capability overlap: data, software; region match: Lazio
     risk: deadline closing soon
     https://www.lazioinnova.it/bandi/donne-e-impresa-2026/
```

`--profile` accepts either a **bundled example name** (`mayai`,
`medtech_lombardia`, `pmi_toscana`, … — packaged in the wheel, so the demos work
from a `pip install` too) or a **path** to your own profile YAML.

Add `--json` for machine-readable output. Live opportunities come from the
key-less sources (incentivi, TED, `anac_pvl` open tenders, Lombardia, Lazio);
`anac` adds historical awarded-contract data (see [Sources](#sources) and
[Status](#status)).

## Works across company types

BandiRadar isn't tuned to one company — it runs any profile against every source.
`bandiradar batch` runs the bundled profile suite and compares results. Real
output on `--sample` (offline heuristic):

```text
PROFILE                          #  TOP MATCH (score)                      BY SOURCE
------------------------------------------------------------------------------------
Consulenza Strategica S.r.l.    11  Avviso Trasformazioni - Servizi… (55)  incentivi:5 lazio:5 ted:1
Costruzioni Lombarde S.r.l.      2  LAVORI DI FORMAZIONE MANUTENZIO… (56)  lombardia:1 ted:1
Trattoria & Bottega S.r.l.       4  Manifestazione d'interesse per … (55)  incentivi:2 lazio:2
Manifattura Esempio S.r.l.       2  Voucher 3I - Investire in innov… (36)  incentivi:2
MayAI                            4  Manifestazione d'interesse per … (55)  incentivi:1 lazio:2 ted:1
MedForniture Lombardia S.r.l.    2  FORNITURA DI DISPOSITIVI PER EN… (76)  lombardia:2
Innova Toscana S.r.l.           12  Bando 1.3.2 - Sostegno alle PMI… (60)  incentivi:3 ted:2 toscana:7
Studio Associato Commercialis…   8  Efficienza energetica e rinnova… (52)  incentivi:2 lazio:5 ted:1
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

## Matching quality (measured)

Most matching repos ask you to trust them. This one ships the numbers. On a
**labelled gold set of 312 real opportunities × 8 company profiles**
(`src/bandiradar/data/eval/`), here is the matcher quality — reproduce it any time
with `bandiradar eval --diagnostics` (offline for the heuristic; set an LLM key for
the LLM column):

```text
min_score sweep — precision@5 / precision@10 / recall / false-positive-rate / returned
                 P@5   P@10  recall  FPR    returned
HEURISTIC (offline, zero-secret)
  recall  (0)   0.34  0.20   0.87   0.29     99      ← the firehose
  balanced(20)  0.34  0.20   0.87   0.29     99      ← scores too coarse to move
  precision(40) 0.53  0.39   0.64   0.24     82
  (60)          0.25  0.25   0.03   0.00      2      ← collapses; no usable cut
LLM pointwise (anthropic Haiku)
  recall  (0)   0.37  0.24   0.87   0.29     99      ← the firehose
  balanced(20)  0.46  0.37   0.78   0.11     51
  precision(40) 0.73  0.68   0.45   0.03     26      ← the operating point
  (60)          0.81  0.80   0.41   0.02     20
```

**Read it:** with an LLM, raising the cutoff cleanly trades recall for precision —
at `precision` (min_score ≥ 40) **P@5 0.73 / P@10 0.68 / FPR 0.03**, roughly double
the precision of the unfiltered firehose (**P@5 0.37 / FPR 0.29**) while still
holding ~half the recall. The **offline heuristic** is a genuine zero-secret
fallback (P@5 0.34) but its scores are too coarse to threshold — it has no usable
precision cut (it collapses to 2 items at 60). So **the LLM is the matcher to ship**,
and precision modes are meaningful **only with a key**; keyless runs are
recall-oriented whatever the mode.

### Operating-point modes

`match` / `watch` / `batch` (and the MCP `search_opportunities`) take a `--mode`:

| mode | cutoff | with an LLM key | use it for |
|------|--------|-----------------|------------|
| `precision` | `min_score ≥ 40` | P@5 0.73, P@10 0.68, FPR 0.03 | a tight shortlist |
| `balanced` *(default)* | `min_score ≥ 20` | P@5 0.46, recall 0.78 | day-to-day |
| `recall` | everything prefiltered | recall 0.87 | the monitor's safety net |

`--min-score N` still works for power users (it overrides `--mode`).

### Honest limits (also measured — `eval --diagnostics`)

- **Embeddings** (semantic prefilter, the `embeddings` extra) are **built and
  measured but net-negative** at the current recall ceiling: ~+0.02 recall for a
  1.2–2.7× larger candidate set and higher FPR, so they ship **optional and off**.
- **Recall ceiling is real.** Gate attribution shows the few relevant items the
  prefilter drops are **4/6 correctly-closed bandi** (the deadline gate is right —
  expired calls shouldn't surface) and only **2** a lexical gap; no over-strict gate
  to tune.
- **Listwise reranking** (`eval --rerank`) is an **optional cheaper top-k mode**
  (one LLM call/profile vs N) that lifts top-k slightly but loses the calibrated
  thresholding — so pointwise stays the default.

## Sources

| Source | What it delivers | Live fetch |
|---|---|---|
| **`incentivi`** | incentivi.gov.it (MIMIT) — the national catalogue of **business incentives / grants** (`kind="incentive"`), national and regional. The grant side, and the source a digital SME profile actually matches. | ✅ Wired — the official IODL open-data export, no API key. |
| **`ted`** | TED — Tenders Electronic Daily, the EU's portal for **above-threshold, OPEN, biddable tenders** (includes large Italian public tenders). | ✅ Wired — anonymous, no API key. |
| **`anac_pvl`** | ANAC *Pubblicità a Valore Legale* — the national feed of **OPEN Italian public tenders** (`kind="tender"`), incl. **sub-threshold** ones TED never lists; notices stay online until their deadline. This is the live open-calls feed the others lack. Carries buyer, oggetto, CIG, importo (sparse), CPV, region. CPV labels are resolved to official **8-digit CPV codes** (EU vocabulary; often coarse division-level); region is resolved province→comune(ISTAT)→buyer→national. *Caveats:* importo often absent; CPV codes can be coarse. | ✅ Wired — public JSON API, **no credentials**; keeps only still-open gare (deadline in the future). |
| **`lombardia`** | Regione Lombardia — **regional / sub-threshold** public tenders (`kind="tender"`), from the *Osservatorio Regionale* (Socrata SODA). Carries CPV, value, and province. | ✅ Wired — Socrata SODA, no API key. |
| **`lazio`** | Regione Lazio — **regional business incentives** (`kind="incentive"`), from the LazioInnova bandi portal (WordPress REST API). The source the MayAI dogfood profile matches. | ✅ Wired — WP REST, no API key. |
| **`toscana`** | Regione Toscana — **regional business incentives** (`kind="incentive"`), from the Sviluppo Toscana bandi portal. First **LLM-assisted scraper**: the portal has no field API, so an LLM extracts the canonical fields from each bando page. | ⚠️ Wired — live `fetch()` **needs an LLM key**; fields are extracted from the portal's HTML bando pages. `--sample` replays a recorded extraction offline. |
| **`anac`** | ANAC / PNCP open-contracting (OCDS) data — **historical / awarded contracts** (> €40k, monthly), not open calls. Surfaces mostly-**closed** opportunities (the matcher drops them); its value is market/history analysis. Region is absent in the data → `national`. | ✅ Wired — streams the Open Contracting mirror (CC BY 4.0, no API key), **capped** at 500 releases/run. |

```bash
uv run bandiradar fetch --source incentivi --sample   # offline, bundled real capture
uv run bandiradar match --profile mayai --source incentivi --sample
uv run bandiradar match --profile mayai --source ted --sample
```

The `--sample` fixtures are **real captures** (`data/fixtures/*.json`).
`incentivi` exercises the canonical superset on the grant side — no CPV, a funding
range, and an eligibility text the matcher reads. TED carries above-threshold
contracts often far larger than a micro-SME's range, so a small profile matches
only the few that fit — which is why incentive/national/regional sources matter too.

A regional example (the bundled `medtech_lombardia` example profile, a Lombardy
medical-devices distributor) matches open Lombardy tenders, while the Lazio-only
MayAI profile correctly drops them — regional filtering in action:

```bash
uv run bandiradar match --profile medtech_lombardia --source lombardia --sample
# -> 3 open medical-device tenders (region match, CPV 33*, within value range)
uv run bandiradar match --profile mayai --source lombardia --sample
# -> No matching opportunities (Lazio profile, Lombardy bandi dropped on region)
```

And the dogfood closes the loop — MayAI **is** a Lazio company, and `lazio`
(LazioInnova) is where it finally matches its own region:

```bash
uv run bandiradar match --profile mayai --source lazio --sample
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

For those API-less portals, `toscana` is the reference **LLM-assisted scraper**:
`fetch()` lists each bando's detail page, fetches the HTML, and an LLM extracts the
canonical fields (title, deadline, eligibility, amounts, keywords), cached per URL.
That extraction is I/O, so it lives in `fetch()` and **needs an LLM key** —
`to_opportunities` stays pure, and `--sample` replays a recorded extraction with
**zero secrets**:

```bash
uv run bandiradar fetch --source toscana --sample   # offline, recorded extraction
uv run bandiradar match --profile pmi_toscana --source toscana --sample
# -> Bando Energia Imprese (92), Bando 1.3.2 Sostegno alle PMI/BEI (82),
#    Bando Energia Immobili Imprese (78); public-only Energia Pubblico dropped to 15
```

## Self-healing crawl

![A scraper's listing drifts and the crawl re-derives its own recipe — offline](docs/self-heal.gif)

A scraper's fragile part is the **crawl** (the listing it depends on), not the
extraction — the LLM already adapts to changed HTML. So the crawl is **data, not
code**: a `CrawlRecipe` (where the listing is + dotted paths to each field). That
makes drift detectable and the fix machine-checkable:

1. **Spine** — every healthy crawl validates its results and **snapshots the
   last-good ones** (the *golden*). A drift (renamed/moved fields → unusable refs)
   is detected deterministically, not by a crash.
2. **Healer** — on drift, an LLM is shown one live listing item and the broken
   recipe, and asked to re-derive **only the paths** (data, never code).
3. **Guard** — the candidate recipe is **adopted only if it exactly reproduces the
   golden**. If it parses but differs (content genuinely changed) or stays broken,
   the recipe is left untouched and the source is **flagged for a human** — never a
   silent swap. Adoptions are auditable (`{recipe, adopted_at, reason, validated_by}`).

```bash
uv run python scripts/demo_self_heal.py   # offline, fake healer: drift → heal → recovered
```

First demonstrated on the `toscana` scraper. This keeps a scraper alive across
small portal changes without shipping new code — and refuses to guess when it
can't prove the fix. Where the open engine stops and managed/premium coverage
begins is mapped in the **[coverage map](docs/coverage-map.md)**.

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
uv run bandiradar match --profile mayai --source ted --sample --with-benchmarks
```

```text
#1  score 44  [open]  Italia – Servizi di gestione dati – SERVIZIO DI GESTIONE ... ROCCA IMPERIALE (CS)
     why: CPV prefix match (depth 2); capability overlap: dati; eu scope; ANAC history (CPV 72, national): 8 awards, median EUR 104,326, p25-p75 EUR 71,619-183,410
     https://ted.europa.eu/en/notice/-/detail/376324-2026
```

Value-sanity triggers when the opportunity declares a value — e.g. a Lombardy
medical-devices tender (`--profile medtech_lombardia --source
lombardia --sample --with-benchmarks`):

```text
#1  score 76  [open]  FORNITURA DI DISPOSITIVI PER ENDOSCOPIA DIGESTIVA … ASST LODI …
    why: ...; ANAC history (CPV 33, national): 7 awards, median EUR 1,183,540, p25-p75 EUR 104,190-1,640,000
    risk: estimated value EUR 2,133,178 is above the historical p75 EUR 1,640,000 for this category
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
uv run bandiradar watch --profile mayai --source incentivi --sample
# write a feed instead of printing
uv run bandiradar watch --profile mayai --source incentivi --sample --rss ~/feed.xml
```

`export` is the full, non-delta dump of current matches (`--json` or `--rss PATH`).

**Scheduling is your cron** — this is open-core (single-user/local). For example:

```cron
0 8 * * *  cd /path/to/bandiradar && uv run bandiradar watch --profile mine.yaml --rss ~/feed.xml
```

Managed delivery (WhatsApp/email/alerts), scheduling SaaS, and multi-tenant
hosting live in `bandiradar-pro`.

### Live monitor (runs itself, daily)

This repo monitors itself. A GitHub Actions workflow
([`.github/workflows/monitor.yml`](.github/workflows/monitor.yml)) runs **every day
at 06:00 UTC** (and on demand): it fetches every key-less source — plus `toscana`,
so the [self-healing crawl](#self-healing-crawl) drift-check runs in production —
watches **every bundled profile**, and publishes the results to the orphan
[**`monitor-data`** branch](../../tree/monitor-data):

- `feeds/<profile>.xml` / `feeds/<profile>.json` — the new/amended matches per profile;
- [`STATUS.md`](../../blob/monitor-data/STATUS.md) — run date, per-source outcome +
  counts, new matches per profile, and the crawl-recipe state (`ok` / `drift` /
  `healed` / `flagged`).

It runs **with zero secrets** (guardrail 1): keyless ⇒ recall mode + offline
heuristic matcher, and crawl drift is only *detected*. Add the optional
`ANTHROPIC_API_KEY` repo secret and the same workflow scores with the LLM **and**
activates the crawl **healer** (a drifted recipe is auto-re-derived and adopted only
if it reproduces the golden exactly). The data branch is kept **flat** — one
force-pushed commit per run, so generated state never bloats the repo history. A run
fails only when **every** source fails; partial failures are warnings in `STATUS.md`.

It fetches **once per run** (the first profile fetches every source; the others
reuse the DB via `watch --skip-fetch`), so the whole job takes **~5 minutes**, not
30+. Every request sends an identifying `User-Agent` and a short connect timeout.

**Operational protections (v0.5.1).** With an LLM key, a per-run spend cap
(`BANDIRADAR_LLM_BUDGET`) bounds new scorings — items beyond the cap are *deferred*
and amortized by the score cache across the next runs, not dropped. Before each
publish, `bandiradar prune` trims stale `raw_docs` of long-closed opportunities and
old run rows (then `VACUUM`s) to keep the data branch well under GitHub's blob limit,
without touching the score cache or crawl recipes. And the long step is time-boxed so
doctor + STATUS + publish always run: if the run is **truncated**, `STATUS.md` says so
(`⚠️ Run truncated: X/N profiles completed`) instead of republishing stale numbers.

> **Known limit — incentivi.gov.it from CI.** `incentivi.gov.it` is open and
> documented, but its export endpoint is **unreachable from GitHub-hosted runners**:
> the connection times out (`ConnectTimeout`) because the site's firewall drops
> Azure datacenter IP ranges at the connection level. This is **not fixable in
> code** — the endpoint works fine from residential IPs / local runs (verified) — so
> we accept it: the source is classified **`unavailable`** in `STATUS.md` and **every
> other source still runs**. A pre-flight step in the workflow curls the incentivi
> (and TED) endpoints and logs the HTTP status so the block is visible upfront.
> *(TED's earlier 403 from CI was a different issue — a default-User-Agent block — and
> is **fixed**: with our identifying User-Agent, TED now fetches from the runners.)*

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
- ✅ **6 live key-less sources** — `incentivi`, `ted`, `anac_pvl`, `lombardia`,
  `lazio` (open calls) plus `anac` (historical). `--sample` keeps them offline
  against recorded real captures.
- ✅ **Live OPEN Italian tenders** — `anac_pvl` (Pubblicità a Valore Legale) is the
  national feed of open, biddable gare, incl. sub-threshold ones TED never lists, no
  credentials; it keeps only still-open notices.
- ✅ **1 LLM-assisted scraper** — `toscana`: live `fetch()` extracts fields from the
  portal's HTML bando pages with an LLM (needs a key); `--sample` replays a recorded
  extraction with zero secrets.
- ✅ **Self-healing crawl** — a drifted scraper listing triggers an LLM that
  re-derives the crawl recipe (data, not code); it's adopted only when it exactly
  reproduces the last-good results, else human-flagged.
- ✅ **Stage-2 LLM scoring is wired and working** (optional); with no key it
  transparently uses the offline heuristic — a proxy, not real semantic relevance.
- ✅ **Live ANAC/PNCP fetch is wired** — streams the Open Contracting OCDS mirror
  (key-less), capped at 500 releases/run. The data is **retrospective** (awarded
  contracts), so it surfaces mostly-closed opportunities — useful for history /
  market analysis, not as a feed of open calls.
- ✅ **Live-fetch robustness shipped (0.2.0)** — retries/backoff, pagination,
  per-source isolation (one source failing never aborts the others), per-record
  quarantine, and a `doctor` diagnostic. Dirty single records are tolerated, never
  fatal.
- ⏳ **Honest limitation:** the real residual gap is **coverage**, not robustness —
  Italian regional funding is fragmented across bespoke API-less portals, and the
  richest tender documents are gated. See the
  **[coverage map](docs/coverage-map.md)** for the open-vs-gated landscape and where
  the open/Pro boundary falls.

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
  incentives), **`anac_pvl`** (national OPEN tenders — Pubblicità a Valore Legale,
  incl. sub-threshold), **Regione Lombardia** (CKAN/Socrata tenders) and **Regione
  Lazio** (LazioInnova incentives), all key-less; **ANAC OCDS** wired as a capped,
  key-less historical / awarded-contracts feed (analysis, not open calls).
- **CPV resolver** (Italian CPV labels → official 8-digit EU codes) + region
  fallback (province → comune/ISTAT → buyer → national) — measured keyless recall
  gains on tender profiles.
- **LLM-assisted scraper** for API-less regional portals — **Regione Toscana**
  (Sviluppo Toscana) is the first instance (live fetch needs an LLM key).
- **Self-healing crawl** — crawl recipes as data + drift detection + golden-sample
  guard + an LLM recipe healer (gated adoption; human-flagged otherwise).
- **[Coverage map](docs/coverage-map.md)** — honest open-vs-gated landscape of
  Italian funding data.
- **Intelligence track:** ANAC historical benchmarks + optional matcher
  enrichment (`--with-benchmarks`).
- **`watch` monitor loop** (new/amended deltas) + **JSON/RSS export**.
- **Embeddings semantic prefilter** — built and **measured; ships optional and off**
  (net-negative at the current recall ceiling — see *Honest limits* under
  [Matching quality](#matching-quality-measured)).

**Upcoming**
- More community/regional source adapters (via the `Source` framework — Lombardy is
  the first; other regions welcome).
- `bandiradar-pro` (private): dashboard, WhatsApp/email delivery, scheduling
  SaaS, multi-tenant hosting.

## Contributing

Every source is `fetch` + a pure `to_opportunities`, plus a recorded fixture and
a test — adding one is a new file, no core changes. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) and the `add-a-source` skill
(`skills/add-a-source/`) for the full copy-pasteable template; the playbook also
lives in `CLAUDE.md` ("How to add a new Source").

Each source also has an offline **contract test** against a recorded real response
(`tests/cassettes/`), plus an **opt-in live drift check** that runs only with
`uv run pytest -m live` (never in CI). See [`CONTRIBUTING.md`](CONTRIBUTING.md) for
how to run it and re-record a cassette when an API changes.

## License

MIT © MayAI — see [`LICENSE`](LICENSE).

## Data and licenses

BandiRadar consumes public open data; each source keeps its own licence, which
its operator requires you to honor:

- **TED — Tenders Electronic Daily (EU)** — the EU's public procurement journal
  (Publications Office of the EU). Notice data is reusable under the Commission's
  open-data reuse policy; the live `ted` fetch uses the anonymous public
  `api.ted.europa.eu` search API (no key). Attribute TED / the Publications Office.
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
- **Regione Toscana / Sviluppo Toscana** — bandi published on the Sviluppo Toscana
  portal (`sviluppo.toscana.it`); detail-page links come from its WP REST listing
  and the fields are LLM-extracted from each public bando page. Source: Sviluppo
  Toscana / Regione Toscana; attribute the source when reusing.
- **ANAC public contracts (CC BY 4.0)** — via the
  [Open Contracting Data mirror](https://data.open-contracting.org/en/publication/117/);
  both the `anac` source and the intelligence track stream the gzipped JSONL
  memory-safely (line by line) through the shared reader in `bandiradar/ocp.py`.
