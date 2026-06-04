"""Typer CLI — a THIN shell over ``core`` (ARCHITECTURE.md §9).

Commands: profile, sources, fetch, match, watch, mcp. Defaults to offline sample
mode; --sample never needs secrets. Contains NO business logic — every command
calls into ``core`` (or the registry) and only formats output. Real errors exit
non-zero.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import typer

from bandiradar import core, exporters
from bandiradar.intelligence import anac_history
from bandiradar.intelligence import benchmarks as bench
from bandiradar.intelligence.store import BenchmarkStore
from bandiradar.sources.base import list_sources

app = typer.Typer(
    name="bandiradar",
    help="Monitor Italian public funding opportunities and rank them for your company.",
    no_args_is_help=True,
)
profile_app = typer.Typer(help="Inspect and validate company profiles.")
sources_app = typer.Typer(help="Inspect available sources.")
benchmarks_app = typer.Typer(help="ANAC historical benchmarks (intelligence track).")
app.add_typer(profile_app, name="profile")
app.add_typer(sources_app, name="sources")
app.add_typer(benchmarks_app, name="benchmarks")

_DEADLINE_FMT = "%Y-%m-%d"


def _fmt_deadline(opp) -> str:
    return opp.deadline.strftime(_DEADLINE_FMT) if opp.deadline else "—"


def _print_ranked(ranked) -> None:
    """Human-readable ranked match list (shared by `match` and `watch`)."""
    for rank, (opp, m) in enumerate(ranked, start=1):
        issuer = opp.issuer_name or "—"
        region = opp.region or opp.issuer_region or "—"
        typer.echo(f"#{rank}  score {m.score}  [{opp.status}]  {opp.title}")
        typer.echo(f"     issuer: {issuer} ({region})   deadline: {_fmt_deadline(opp)}")
        if m.reasons:
            typer.echo(f"     why: {'; '.join(m.reasons)}")
        if m.risk_notes:
            typer.echo(f"     risk: {'; '.join(m.risk_notes)}")
        typer.echo(f"     {opp.source_url}")
        typer.echo("")


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
    with_benchmarks: bool = typer.Option(
        False, "--with-benchmarks", help="Add ANAC historical benchmark notes"
    ),
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
            with_benchmarks=with_benchmarks,
        )
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()

    if json_out:
        typer.echo(exporters.to_json(ranked))
        return

    if not ranked:
        typer.echo("No matching opportunities.")
        return

    noun = "opportunity" if len(ranked) == 1 else "opportunities"
    typer.echo(f"{len(ranked)} matching {noun} for '{company.name}':\n")
    _print_ranked(ranked)


# --------------------------------------------------------------------------- #
# benchmarks (intelligence track)
# --------------------------------------------------------------------------- #


@benchmarks_app.command("build")
def benchmarks_build(
    sample: bool = typer.Option(False, "--sample", help="Use bundled offline fixture"),
    year: int = typer.Option(2025, "--year", help="Dataset year (live mode)"),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default: env/home)"),
):
    """Ingest ANAC history and write (CPV-division x region) benchmarks."""
    store = BenchmarkStore(db)
    try:
        result = anac_history.build_benchmarks(sample=sample, year=year, store=store)
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()
    typer.echo(f"records={result['records']} benchmarks={result['benchmarks']}")


@benchmarks_app.command("show")
def benchmarks_show(
    cpv: str = typer.Option(..., "--cpv", help="CPV division (2 digits, e.g. 72)"),
    region: str | None = typer.Option(
        None, "--region", help="Region (falls back to national)"
    ),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default: env/home)"),
    json_out: bool = typer.Option(False, "--json", help="JSON output"),
):
    """Show a benchmark for a CPV division (region falls back to national)."""
    store = BenchmarkStore(db)
    try:
        result = bench.lookup(store, cpv, region)
    finally:
        store.close()

    if result is None:
        typer.echo(f"No benchmark for CPV division {cpv!r}.")
        raise typer.Exit(1)

    if json_out:
        typer.echo(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)
        )
        return

    scope = result.region or "national"
    typer.echo(f"CPV division {result.cpv_division}  [{scope}]")
    typer.echo(
        f"  awards (count): {result.count}   "
        f"distinct suppliers: {result.distinct_suppliers}"
    )
    typer.echo(
        f"  value EUR: median {result.value_median:,.0f}  "
        f"p25 {result.value_p25:,.0f}  p75 {result.value_p75:,.0f}"
    )
    typer.echo(f"  range: {result.value_min:,.0f} – {result.value_max:,.0f}")
    by_year = "  ".join(f"{y}:{n}" for y, n in sorted(result.by_year.items()))
    typer.echo(f"  by year: {by_year}")


# --------------------------------------------------------------------------- #
# watch / export
# --------------------------------------------------------------------------- #


def _source_ids(source: str | None) -> list[str] | None:
    return [s.strip() for s in source.split(",") if s.strip()] if source else None


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--since must be ISO date/datetime: {value!r}"
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _filter_sources(ranked, source_ids):
    if not source_ids:
        return ranked
    wanted = set(source_ids)
    return [(o, m) for o, m in ranked if o.source in wanted]


@app.command()
def watch(
    profile: str = typer.Option(..., "--profile", help="Path to a profile YAML"),
    source: str | None = typer.Option(
        None, "--source", help="Comma-separated source ids (default: all)"
    ),
    sample: bool = typer.Option(False, "--sample", help="Use bundled offline fixture"),
    since: str | None = typer.Option(
        None, "--since", help="Override marker: only items changed after this date"
    ),
    with_benchmarks: bool = typer.Option(
        False, "--with-benchmarks", help="Add ANAC historical benchmark notes"
    ),
    json_out: bool = typer.Option(False, "--json", help="JSON to stdout"),
    rss: str | None = typer.Option(None, "--rss", help="Write RSS feed to PATH"),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default: env/home)"),
):
    """Show NEW or AMENDED matches since the last watch run (a monitor loop).

    Scheduling is your cron, e.g.:
        0 8 * * *  bandiradar watch --profile mine.yaml --rss ~/feed.xml
    Managed delivery (WhatsApp/email/alerts) lives in bandiradar-pro.
    """
    store = core.Store(db)
    try:
        company = core.load_profile(profile)
        delta = core.run_watch(
            company,
            store,
            source_ids=_source_ids(source),
            sample=sample,
            since=_parse_since(since),
            with_benchmarks=with_benchmarks,
        )
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()

    if rss is not None:
        Path(rss).write_text(exporters.to_rss(delta), encoding="utf-8")
        typer.echo(f"wrote RSS feed: {rss} ({len(delta)} items)")
    if json_out:
        typer.echo(exporters.to_json(delta))
    if not rss and not json_out:
        if not delta:
            typer.echo("No new or amended matches since the last watch.")
        else:
            noun = "match" if len(delta) == 1 else "matches"
            typer.echo(f"{len(delta)} new/amended {noun} for '{company.name}':\n")
            _print_ranked(delta)


@app.command()
def export(
    profile: str = typer.Option(..., "--profile", help="Path to a profile YAML"),
    source: str | None = typer.Option(
        None, "--source", help="Comma-separated source ids (default: all)"
    ),
    sample: bool = typer.Option(False, "--sample", help="Use bundled offline fixture"),
    with_benchmarks: bool = typer.Option(
        False, "--with-benchmarks", help="Add ANAC historical benchmark notes"
    ),
    json_out: bool = typer.Option(False, "--json", help="JSON to stdout"),
    rss: str | None = typer.Option(None, "--rss", help="Write RSS feed to PATH"),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default: env/home)"),
):
    """Full (non-delta) export of the current matches as JSON or RSS."""
    if not json_out and rss is None:
        typer.secho(
            "Choose an output: --json or --rss PATH", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(1)

    source_ids = _source_ids(source)
    store = core.Store(db)
    try:
        company = core.load_profile(profile)
        ranked = core.run_match(
            company,
            store,
            source_id=source_ids[0] if source_ids and len(source_ids) == 1 else None,
            sample=sample,
            with_benchmarks=with_benchmarks,
        )
        ranked = _filter_sources(ranked, source_ids)
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()

    if rss is not None:
        Path(rss).write_text(exporters.to_rss(ranked), encoding="utf-8")
        typer.echo(f"wrote RSS feed: {rss} ({len(ranked)} items)")
    if json_out:
        typer.echo(exporters.to_json(ranked))


# --------------------------------------------------------------------------- #
# batch (profile suite comparison)
# --------------------------------------------------------------------------- #


def _trunc(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


@app.command()
def batch(
    profiles_dir: str = typer.Option(
        "data/profiles", "--profiles-dir", help="Directory of profile YAMLs"
    ),
    source: str | None = typer.Option(
        None, "--source", help="Comma-separated source ids (default: all)"
    ),
    sample: bool = typer.Option(False, "--sample", help="Use bundled offline fixture"),
    min_score: int = typer.Option(0, "--min-score", help="Drop matches below N"),
    top: int | None = typer.Option(None, "--top", help="Keep top K per profile"),
    with_benchmarks: bool = typer.Option(
        False, "--with-benchmarks", help="Add ANAC historical benchmark notes"
    ),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default: env/home)"),
    json_out: bool = typer.Option(False, "--json", help="JSON to stdout"),
    csv_path: str | None = typer.Option(None, "--csv", help="Write CSV to PATH"),
):
    """Run every profile in a directory against the sources and compare results."""
    paths = sorted(Path(profiles_dir).glob("*.yaml"))
    if not paths:
        typer.secho(f"No profiles in {profiles_dir!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    store = core.Store(db)
    try:
        companies = [core.load_profile(p) for p in paths]
        results = core.run_batch(
            companies,
            store,
            source_ids=_source_ids(source),
            sample=sample,
            min_score=min_score,
            top=top,
            with_benchmarks=with_benchmarks,
        )
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()

    if json_out:
        payload = [
            {
                "profile": p.name,
                "matches": len(ranked),
                "by_source": dict(Counter(o.source for o, _ in ranked)),
                "top": (
                    {
                        "opportunity_id": ranked[0][0].id,
                        "score": ranked[0][1].score,
                        "title": ranked[0][0].title,
                    }
                    if ranked
                    else None
                ),
                "results": exporters.match_payload(ranked),
            }
            for p, ranked in results
        ]
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))

    if csv_path is not None:
        src_ids = sorted(s.id for s in list_sources())
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["profile", "matches", "top_score", "top_title", *src_ids])
            for p, ranked in results:
                by = Counter(o.source for o, _ in ranked)
                top_o = ranked[0] if ranked else None
                writer.writerow(
                    [
                        p.name,
                        len(ranked),
                        top_o[1].score if top_o else "",
                        top_o[0].title if top_o else "",
                        *[by.get(s, 0) for s in src_ids],
                    ]
                )
        typer.echo(f"wrote CSV: {csv_path} ({len(results)} profiles)")

    if not json_out and csv_path is None:
        header = f"{'PROFILE':30} {'#':>3}  {'TOP MATCH (score)':38} BY SOURCE"
        typer.echo(header)
        typer.echo("-" * len(header))
        for p, ranked in results:
            by = Counter(o.source for o, _ in ranked)
            by_str = " ".join(f"{s}:{n}" for s, n in sorted(by.items())) or "—"
            if ranked:
                opp, m = ranked[0]
                top_str = f"{_trunc(opp.title, 32)} ({m.score})"
            else:
                top_str = "—"
            typer.echo(
                f"{_trunc(p.name, 30):30} {len(ranked):>3}  {top_str:38} {by_str}"
            )


# --------------------------------------------------------------------------- #
# mcp
# --------------------------------------------------------------------------- #


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
