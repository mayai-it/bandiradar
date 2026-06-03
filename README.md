# BandiRadar

> Open-source engine that monitors Italian public funding opportunities
> (public tenders, grants, incentives), normalizes them into **one canonical
> model**, and ranks them against a company profile with a two-stage matcher.

**Status:** scaffolding (Phase 0 — v1 spine). Modules are stubbed and importable;
logic is built one vertical slice at a time. See `ARCHITECTURE.md` for the design
and `CLAUDE.md` for the operational contract.

## Quickstart (offline, zero secrets)

```bash
uv sync
uv run pytest
```

Once the slices are implemented, the engine runs end-to-end on bundled sample
data with no API keys:

```bash
uv run bandiradar fetch --source anac --sample
uv run bandiradar match --profile data/profiles/mayai.yaml --sample
```

## Open core

This repository is the **open (MIT)** engine: canonical model, `Source`
framework, reference adapters, two-stage matcher, CLI, and MCP server. The
dashboard, premium/regional adapters, and delivery channels live in the private
`bandiradar-pro`. See `ARCHITECTURE.md §2`.

## License

MIT © MayAI
