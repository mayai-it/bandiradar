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
| **`ted`** | TED — Tenders Electronic Daily, the EU's portal for **above-threshold, OPEN, biddable tenders** (includes large Italian public tenders). | ✅ Wired — anonymous, no API key. |
| **`anac`** | ANAC / PNCP open-contracting (OCDS) data — primarily **historical / award records**, a separate analytics track rather than open calls. | ⏳ Mapper + fixture done; live `fetch()` not wired. |

```bash
uv run bandiradar fetch --source ted --sample      # offline, bundled real capture
uv run bandiradar match --profile data/profiles/mayai.yaml --source ted --sample
```

The TED `--sample` fixture is a **real capture** of Italian IT/software tenders
(`data/fixtures/ted.json`). Note that TED carries above-threshold contracts —
often far larger than a micro-SME's range — so a small profile like MayAI
matches only the few that fit (e.g. an undisclosed-value data-services tender).
That's exactly why national/regional/incentive sources matter too.

## Status (honest)

- ✅ **Runs today fully offline** on bundled sample data with **zero secrets** —
  both quickstarts above are real.
- ✅ **TED has a live, anonymous fetch** (no key) over the EU search API; it is
  the first real source. `--sample` keeps it offline against a recorded capture.
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

- **Phase 0 — v1 spine (this build):** canonical model + Source framework + ANAC
  adapter (sample today, live next) + two-stage matcher with offline fallback +
  SQLite + CLI + MCP + tests. Runs end-to-end on sample data with zero secrets.
- **Phase 1:** `incentivi.gov.it` adapter, `watch`/scheduling, JSON/RSS export,
  embeddings-based prefilter.
- **Phase 2:** community regional adapters (via the `Source` framework) +
  `bandiradar-pro` (dashboard, WhatsApp/email delivery, multi-tenant, hosting).

## Add a source / Contributing

Every source is `fetch` + a pure `to_opportunities`, plus a recorded fixture and
a test — adding one is a new file, no core changes. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) and the `add-a-source` skill
(`skills/add-a-source/`) for the full copy-pasteable template; the playbook also
lives in `CLAUDE.md` ("How to add a new Source").

## License

MIT © MayAI — see [`LICENSE`](LICENSE).
