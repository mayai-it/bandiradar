# Contributing to BandiRadar

Thanks for being here! BandiRadar's mission only works if the **long tail of
Italian funding sources** gets covered — and that's exactly where community
contributions matter most.

## The most valuable contribution: a new source

Italian public funding is scattered across national portals and **dozens of
regional *bandi* sites**. We can't (and shouldn't) scrape every one ourselves —
the architecture is built so that **adding a source is a new file, not a core
change**. If you know a regional or sector-specific source, adding it is the
single highest-impact thing you can do.

### The `Source` contract

Every source implements the same tiny interface (see `ARCHITECTURE.md §5`):

```python
class Source(Protocol):
    id: str
    kind: Literal["tender", "grant", "incentive"]
    def fetch(self, since: datetime | None = None) -> Iterable[RawDoc]: ...
    def to_opportunities(self, raw: RawDoc, now=None) -> list[Opportunity]: ...
```

- `fetch()` pulls raw payloads; `to_opportunities()` is a **pure** mapping from a
  raw record to the canonical `Opportunity` model.
- The mapper does no I/O, so it's fully testable offline.

### Two hard requirements

Every adapter PR must ship:

1. **A fixture** — `data/fixtures/<name>.json`, a recorded real payload (a small
   representative slice is fine). This is what lets the adapter be tested with
   **zero secrets and no network**.
2. **An offline test** — `tests/test_<name>.py` asserting the mapper output
   against that fixture. No source without a test.

### Fastest path: the `add-a-source` skill

`skills/add-a-source/SKILL.md` is a concrete, copy-pasteable template: a skeleton
adapter, the fixture format, and the test. If you use an AI coding agent, point
it at that skill. The same playbook is summarized in `CLAUDE.md`.

### Regional coverage — where help is most needed

[`docs/regions.md`](docs/regions.md) is the live map of which regional bandi
portals have been checked and which still need an adapter. If a region's agency
runs WordPress with a bandi REST endpoint, it's a **config-only** entry on
`WordPressBandiSource` (see the example in that doc). Otherwise it needs a small
dedicated adapter (CKAN/Socrata or HTML scraping). Either way: **real fixture +
offline test, no fabricated data.**

## What belongs here vs. `bandiradar-pro`

This repo is the **open (MIT) engine**. The boundary (ARCHITECTURE.md §2):

- **Here (open):** the engine, the `Source` framework, reference and community
  source adapters, the two-stage matcher, the CLI, and the MCP server.
- **Not here (private `bandiradar-pro`):** the web dashboard, premium/managed
  adapters, delivery channels (WhatsApp/email/alerts), multi-tenant, and hosting.

Rule of thumb: **anything a single user runs locally is open; anything managed,
multi-client, or a delivery channel is pro.** Please don't send dashboard,
delivery, or hosting code here.

## Dev loop

```bash
uv sync                 # install (Python 3.12)
uv run pytest           # tests — must be green OFFLINE, with zero secrets
uv run ruff check .     # lint
uv run ruff format .    # format
```

The full suite must pass **without any API keys or network**. The LLM matcher
has a deterministic offline fallback precisely so CI and contributors never need
secrets.

## Commit & PR conventions

- **Branch** off `main`; keep a PR focused on one source / one change.
- **Conventional-ish commit messages**, e.g. `feat: add <region> bandi source`,
  `fix: ...`, `docs: ...`.
- **Definition of done** (see `CLAUDE.md`): new/changed code has tests; `uv run
  pytest` is green offline; `ruff check` is clean; and if you touched the
  canonical model contract, update `ARCHITECTURE.md` and every adapter + test in
  the same change.
- Describe in the PR **what the source is, where the fixture came from**, and any
  endpoint caveats (if the live `fetch()` can't be wired yet, leave it raising
  `NotImplementedError` with a clear TODO — that's fine, the fixture + mapper are
  what we review).

## Code of conduct

Be kind and constructive. This is a welcoming project for first-time
contributors — questions and work-in-progress PRs are encouraged.

MIT © MayAI
