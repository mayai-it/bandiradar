---
name: bandiradar
description: Drive BandiRadar to find and rank Italian public funding opportunities (tenders, grants, incentives) for a company profile. Use when the user wants to fetch opportunities, match/rank them against a profile, score a specific opportunity, or run the engine offline on sample data — via the CLI or the MCP tools.
---

# Using BandiRadar

BandiRadar monitors Italian public funding opportunities, normalizes them into a
canonical `Opportunity` model, and ranks them against a company **profile** with
a two-stage matcher (deterministic prefilter → LLM relevance, with a zero-secrets
offline fallback).

## Key facts before you start

- **Offline by default.** Pass `--sample` (CLI) or `sample=true` (MCP) to run on
  bundled fixture data with **no API keys**. Always prefer this unless the user
  explicitly wants live data.
- **Live fetch is not wired yet.** Running without `--sample` calls the live
  source `fetch()`, which currently raises `NotImplementedError` (the ANAC/PNCP
  endpoint is unconfirmed). Do not promise live results.
- **A profile is required for matching.** It is a YAML file (see
  `data/profiles/mayai.yaml`, `data/profiles/manifattura.yaml`) describing the
  company: `ateco`, `cpv_interests`, `keywords`, `regions`, `value_range`,
  `capabilities`, `exclusions`.
- **Scores without an LLM key are a deterministic heuristic proxy**, not true
  relevance. For real scoring, the environment needs `BANDIRADAR_LLM_PROVIDER`
  (`anthropic`|`openai`) and the matching API key.

## CLI

Run via `uv run bandiradar <command>`.

| Command | What it does |
|---|---|
| `profile show <path>` | Print a parsed profile as JSON. |
| `profile validate <path>` | Validate a profile file (non-zero exit if invalid). |
| `sources list` | List registered sources (`--json` for JSON). |
| `fetch --source anac --sample [--db PATH]` | Ingest a source into the store; prints `fetched=.. new=.. amended=..`. |
| `match --profile <path> --sample [--source anac] [--min-score N] [--limit N] [--db PATH] [--json]` | Rank opportunities for a profile. |
| `watch` | Phase-1 stub (scheduling lives in bandiradar-pro). |
| `mcp` | Launch the MCP server (stdio). |

**Typical offline run:**

```bash
uv run bandiradar match --profile data/profiles/mayai.yaml --sample
```

`match` auto-fetches the sample data if the store is empty, so a single command
works from a clean checkout. Use a throwaway `--db` (e.g. a temp path) if you
don't want to touch the default database.

### Reading the ranked output

Each result is one opportunity, best first:

```
#1  score 76  [closing_soon]  <title>
     issuer: <issuer> (<region>)   deadline: <YYYY-MM-DD>
     why: <top reasons>
     <source_url>
```

- `score` is `0–100` (higher = more relevant).
- status `closing_soon` / `closed` flags deadline urgency.
- `why` lists the matcher's reasons (CPV match, capability overlap, value fit).

For programmatic use add `--json` → a list of
`{opportunity_id, score, status, title, deadline, reasons, matched_capabilities, source_url}`.

## MCP tools

Start with `uv run bandiradar mcp` (see `docs/MCP.md` to register with Claude).
All tools default to offline sample mode and return canonical fields only.

- `list_sources()` → `[{id, kind}]`.
- `fetch_opportunities(source="anac", sample=true, db=None)` → `{fetched, new, amended}`.
- `search_opportunities(profile_path | profile, source=None, sample=true, min_score=0, limit=None, db=None)`
  → ranked `[{opportunity_id, score, status, title, issuer, region, deadline, reasons, matched_capabilities, source_url}]`.
- `score_opportunity(opportunity_id, profile_path | profile, db=None)` → one `Match`.
- `get_matches(profile_path | profile, min_score=0, limit=None, db=None)` → persisted matches (no recompute).
- `get_profile(profile_path)` → parsed profile dict.

For the profile, pass **either** `profile_path` (a YAML path) **or** an inline
`profile` dict — exactly one.

## Recommended flow

1. Validate or inspect the profile: `profile validate <path>`.
2. Match offline: `match --profile <path> --sample` (or `search_opportunities`).
3. Present the top results: title, score, status, deadline, the `why` reasons,
   and the source URL. Call out `closing_soon` items first.
4. Only attempt live fetch if the user insists — and warn that it is not wired.
