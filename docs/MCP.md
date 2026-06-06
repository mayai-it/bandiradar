# BandiRadar MCP server

BandiRadar ships a thin [MCP](https://modelcontextprotocol.io) server (FastMCP)
so you can drive the engine from Claude. Every tool is a thin wrapper over the
core pipeline, returns only canonical fields (never raw payloads), and works
**offline in sample mode with zero secrets**.

## Run it

```bash
uv run bandiradar mcp        # launches the FastMCP server (stdio)
# equivalent:
uv run python -m bandiradar.mcp_server
```

## Register with Claude

### Claude Code (CLI)

```bash
claude mcp add bandiradar -- uv --directory /absolute/path/to/bandiradar run bandiradar mcp
```

### JSON config (Claude Desktop / editors)

Add to your MCP client config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "bandiradar": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/bandiradar", "run", "bandiradar", "mcp"]
    }
  }
}
```

Set `BANDIRADAR_LLM_PROVIDER` / API keys in the environment only if you want the
LLM matcher; the default offline heuristic needs none.

## Tools

| Tool | Purpose |
|------|---------|
| `list_sources()` | Registered sources `[{id, kind}]`. |
| `fetch_opportunities(source="anac", sample=True, db=None)` | Ingest a source; returns `{fetched, new, amended}`. |
| `search_opportunities(profile_path \| profile, source=None, sample=True, min_score=0, limit=None, db=None)` | Ranked canonical matches for a profile. |
| `score_opportunity(opportunity_id, profile_path \| profile, db=None)` | A single `Match` for one stored opportunity. |
| `get_matches(profile_path \| profile, min_score=0, limit=None, db=None)` | Persisted matches for the profile (no recompute). |
| `get_profile(profile_path)` | The parsed profile dict. |

For the profile, pass **either** `profile_path` (a YAML path) **or** an inline
`profile` dict — exactly one.

## Example session (offline)

```text
> fetch_opportunities(source="lazio", sample=true)
{ "fetched": 15, "new": 15, "amended": 0 }

> search_opportunities(profile_path="data/profiles/mayai.yaml", source="lazio", sample=true)
[
  { "opportunity_id": "lazio:48841", "score": 52, "status": "open",
    "title": "Voucher Digitalizzazione PMI 2025", ... },
  { "opportunity_id": "lazio:58887", "score": 42, "status": "closing_soon",
    "title": "Donne e Impresa 2026", ... }
]
```

(`fetch_opportunities` defaults to `source="anac"`, which is historical / awarded
contracts — mostly closed; pass a key-less source like `lazio` or `incentivi` for
open opportunities.)
