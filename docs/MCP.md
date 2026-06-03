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
> fetch_opportunities(sample=true)
{ "fetched": 6, "new": 6, "amended": 0 }

> search_opportunities(profile_path="data/profiles/mayai.yaml", sample=true)
[
  { "opportunity_id": "anac:ocds-bandi-0002", "score": 76, "status": "closing_soon",
    "title": "Fornitura di licenze software e servizi cloud GDPR-compliant", ... },
  { "opportunity_id": "anac:ocds-bandi-0004", "score": 72, "status": "open", ... },
  { "opportunity_id": "anac:ocds-bandi-0001", "score": 66, "status": "open", ... }
]
```
