"""Typer CLI — a THIN shell over ``core`` (ARCHITECTURE.md §9).

Commands: profile, sources, fetch, match, watch, mcp. Defaults to offline sample
mode; --sample never needs secrets. Contains NO business logic — every command
calls into ``core`` (or the registry) and only formats output. Real errors exit
non-zero.
"""

from __future__ import annotations

import json

import typer

from bandiradar import core
from bandiradar.sources.base import list_sources

app = typer.Typer(
    name="bandiradar",
    help="Monitor Italian public funding opportunities and rank them for your company.",
    no_args_is_help=True,
)
profile_app = typer.Typer(help="Inspect and validate company profiles.")
sources_app = typer.Typer(help="Inspect available sources.")
app.add_typer(profile_app, name="profile")
app.add_typer(sources_app, name="sources")

_DEADLINE_FMT = "%Y-%m-%d"


def _fmt_deadline(opp) -> str:
    return opp.deadline.strftime(_DEADLINE_FMT) if opp.deadline else "—"


# --------------------------------------------------------------------------- #
# profile
# --------------------------------------------------------------------------- #


@profile_app.command("show")
def profile_show(path: str = typer.Argument(..., help="Path to a profile YAML")):
    """Print a profile's parsed fields."""
    try:
        profile = core.load_profile(path)
    except Exception as exc:  # noqa: BLE001 — surface any load/validation error
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2)
    )


@profile_app.command("validate")
def profile_validate(path: str = typer.Argument(..., help="Path to a profile YAML")):
    """Validate a profile file; non-zero exit if invalid."""
    try:
        profile = core.load_profile(path)
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Invalid profile: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.secho(f"OK: '{profile.name}' is a valid profile.", fg=typer.colors.GREEN)


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #


@sources_app.command("list")
def sources_list(json_out: bool = typer.Option(False, "--json", help="JSON output")):
    """List registered sources."""
    sources = [{"id": s.id, "kind": s.kind} for s in list_sources()]
    if json_out:
        typer.echo(json.dumps(sources, ensure_ascii=False))
        return
    for s in sources:
        typer.echo(f"{s['id']:12} {s['kind']}")


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #


@app.command()
def fetch(
    source: str = typer.Option("anac", "--source", help="Source id"),
    sample: bool = typer.Option(False, "--sample", help="Use bundled offline fixture"),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default: env/home)"),
):
    """Fetch a source into the store and print counts."""
    store = core.Store(db)
    try:
        counts = core.run_fetch(source, store, sample=sample)
    except NotImplementedError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()
    typer.echo(
        f"fetched={counts['fetched']} new={counts['new']} amended={counts['amended']}"
    )


# --------------------------------------------------------------------------- #
# match
# --------------------------------------------------------------------------- #


@app.command()
def match(
    profile: str = typer.Option(..., "--profile", help="Path to a profile YAML"),
    source: str | None = typer.Option(None, "--source", help="Limit to a source id"),
    sample: bool = typer.Option(False, "--sample", help="Use bundled offline fixture"),
    min_score: int = typer.Option(0, "--min-score", help="Drop matches below N"),
    limit: int | None = typer.Option(None, "--limit", help="Keep top N"),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default: env/home)"),
    json_out: bool = typer.Option(False, "--json", help="JSON output"),
):
    """Rank opportunities for a profile (offline on --sample)."""
    store = core.Store(db)
    try:
        company = core.load_profile(profile)
        ranked = core.run_match(
            company,
            store,
            source_id=source,
            sample=sample,
            min_score=min_score,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()

    if json_out:
        payload = [
            {
                "opportunity_id": opp.id,
                "score": m.score,
                "status": opp.status,
                "title": opp.title,
                "deadline": opp.deadline.isoformat() if opp.deadline else None,
                "reasons": m.reasons,
                "matched_capabilities": m.matched_capabilities,
                "source_url": opp.source_url,
            }
            for opp, m in ranked
        ]
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not ranked:
        typer.echo("No matching opportunities.")
        return

    noun = "opportunity" if len(ranked) == 1 else "opportunities"
    typer.echo(f"{len(ranked)} matching {noun} for '{company.name}':\n")
    for rank, (opp, m) in enumerate(ranked, start=1):
        issuer = opp.issuer_name or "—"
        region = opp.region or opp.issuer_region or "—"
        typer.echo(f"#{rank}  score {m.score}  [{opp.status}]  {opp.title}")
        typer.echo(f"     issuer: {issuer} ({region})   deadline: {_fmt_deadline(opp)}")
        if m.reasons:
            typer.echo(f"     why: {'; '.join(m.reasons[:3])}")
        typer.echo(f"     {opp.source_url}")
        typer.echo("")


# --------------------------------------------------------------------------- #
# watch / mcp
# --------------------------------------------------------------------------- #


@app.command()
def watch():
    """(Phase 1) Scheduled monitoring — not wired in the open core yet."""
    typer.secho(
        "watch is a Phase-1 feature and is not wired in the open core. "
        "For now, run `bandiradar fetch` then `bandiradar match` on a schedule "
        "(scheduling/delivery live in bandiradar-pro).",
        fg=typer.colors.YELLOW,
    )


@app.command()
def mcp():
    """Launch the MCP server (implemented in Prompt 7)."""
    try:
        from bandiradar import mcp_server
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Error importing MCP server: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    runner = getattr(mcp_server, "main", None) or getattr(mcp_server, "run", None)
    if runner is None:
        typer.secho(
            "MCP server is not implemented yet (see Prompt 7).",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)
    runner()


@profile_app.callback()
def _profile_root() -> None:
    """Profile commands."""


def main() -> None:
    """Console-script entry point (``bandiradar``)."""
    app()
