# CLAUDE.md — bandiradar (open core)

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
    anac.py        # ANAC/PNCP (OCDS) adapter + fixture
  matching/
    prefilter.py   # Stage 1: pure deterministic filter
    relevance.py   # Stage 2: LLM scorer (+ offline fallback + cache)
    llm.py         # provider-agnostic LLM client
    prompts.py     # prompt templates
  storage.py       # SQLite store, dedupe, change detection
  core.py          # service layer that orchestrates the pipeline
  cli.py           # Typer CLI (thin)
  mcp_server.py    # FastMCP server (thin)
data/
  fixtures/        # recorded source payloads for offline tests
  profiles/        # example profiles (mayai.yaml, manifattura.yaml)
tests/
```
Interfaces (`cli.py`, `mcp_server.py`) are THIN — no business logic. All logic
lives in `core.py`, `sources/`, `matching/`, `storage.py`.

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
uv run bandiradar match --profile data/profiles/mayai.yaml --sample
uv run bandiradar mcp        # start MCP server
```

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
3. Record a real payload into `data/fixtures/<name>.json`.
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
