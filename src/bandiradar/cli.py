"""Typer CLI — a THIN shell over ``core`` (ARCHITECTURE.md §9).

Commands: profile, sources, fetch, match, watch, mcp. Defaults to offline sample
mode; --sample never needs secrets. Contains NO business logic — every command
calls into ``core`` (or the registry) and only formats output. Real errors exit
non-zero.
"""

from __future__ import annotations

import csv
import json
import logging
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import typer

from bandiradar import core, exporters
from bandiradar.intelligence import anac_history
from bandiradar.intelligence import benchmarks as bench
from bandiradar.intelligence.store import BenchmarkStore
from bandiradar.models import SourceResult
from bandiradar.sources.base import list_sources

app = typer.Typer(
    name="bandiradar",
    help="Monitor Italian public funding opportunities and rank them for your company.",
    no_args_is_help=True,
)


def _setup_logging(verbose: bool) -> None:
    """Configure stdlib logging to stderr — DEBUG with ``-v``, else INFO.

    Idempotent (won't duplicate handlers or fight pytest's caplog). No secrets are
    logged anywhere; messages carry context (source, counts, page).
    """
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            stream=sys.stderr,
            format="%(levelname)s %(name)s: %(message)s",
        )
    root.setLevel(level)
    logging.getLogger("bandiradar").setLevel(level)


@app.callback()
def _root(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Verbose (DEBUG) logging to stderr."
    ),
) -> None:
    """Monitor Italian public funding opportunities and rank them for your company."""
    _setup_logging(verbose)


_progress_logger = logging.getLogger("bandiradar.progress")


def _progress_sink(quiet: bool = False):
    """Progress sink: per-page lines become INFO log records (None when quiet)."""
    if quiet:
        return None
    return _progress_logger.info


def _source_results_json(results: list[SourceResult]) -> str:
    return json.dumps(
        [r.model_dump(mode="json") for r in results], ensure_ascii=False, indent=2
    )


def _source_summary(results: list[SourceResult]) -> str:
    """A compact per-source table: source | status | new | amended | skip | error."""
    lines = [
        f"{'SOURCE':14} {'STATUS':8} {'NEW':>4} {'AMEND':>5} {'SKIP':>4}  ERROR",
        "-" * 64,
    ]
    for r in results:
        err = r.error or "—"
        if len(err) > 48:
            err = err[:47] + "…"
        lines.append(
            f"{_trunc(r.source, 14):14} {r.status:8} {r.new:>4} "
            f"{r.amended:>5} {r.skipped_invalid:>4}  {err}"
        )
    return "\n".join(lines)


profile_app = typer.Typer(help="Inspect and validate company profiles.")
sources_app = typer.Typer(help="Inspect available sources.")
benchmarks_app = typer.Typer(help="ANAC historical benchmarks (intelligence track).")
app.add_typer(profile_app, name="profile")
app.add_typer(sources_app, name="sources")
app.add_typer(benchmarks_app, name="benchmarks")

_DEADLINE_FMT = "%Y-%m-%d"


def _cutoff_kwargs(mode: str, min_score: int | None) -> dict:
    """Resolve the score cutoff for a run: an explicit ``--min-score`` wins, else the
    ``--mode`` operating point. Returns the right kwarg for ``core.run_*``."""
    return {"min_score": min_score} if min_score is not None else {"mode": mode}


def _fmt_deadline(opp) -> str:
    return opp.deadline.strftime(_DEADLINE_FMT) if opp.deadline else "—"


def _print_ranked(ranked, show_change: bool = False) -> None:
    """Human-readable ranked match list (shared by `match` and `watch`).

    With ``show_change`` (the watch delta), each item is tagged NEW/AMENDED from its
    ``version`` — the change-state lives there now, not in the lifecycle ``status``.
    """
    for rank, (opp, m) in enumerate(ranked, start=1):
        issuer = opp.issuer_name or "—"
        region = opp.region or opp.issuer_region or "—"
        change = ""
        if show_change:
            change = "[AMENDED] " if opp.version > 1 else "[NEW] "
        typer.echo(f"#{rank}  {change}score {m.score}  [{opp.status}]  {opp.title}")
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
    source: str | None = typer.Option(
        None, "--source", help="Source id(s), comma-separated (default: anac)"
    ),
    sample: bool = typer.Option(False, "--sample", help="Use bundled offline fixture"),
    limit: int | None = typer.Option(
        None, "--limit", help="Max records to fetch (live; default safety cap)"
    ),
    max_pages: int | None = typer.Option(
        None, "--max-pages", help="Max pages to fetch (live safety bound)"
    ),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default: env/home)"),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the structured SourceResult list"
    ),
):
    """Fetch one or more sources into the store, with per-source isolation.

    Prints a per-source summary; exit code reflects the worst outcome. One source
    failing never aborts the others.
    """
    source_ids = _source_ids(source) or ["anac"]
    store = core.Store(db)
    try:
        results = core.run_fetch_many(
            source_ids,
            store,
            sample=sample,
            limit=limit,
            max_pages=max_pages,
            progress=_progress_sink(json_out),
        )
    finally:
        store.close()

    if json_out:
        typer.echo(_source_results_json(results))
    else:
        typer.echo(_source_summary(results))

    code = core.fetch_exit_code(results)
    if code:
        raise typer.Exit(code)


# --------------------------------------------------------------------------- #
# match
# --------------------------------------------------------------------------- #


@app.command()
def match(
    profile: str = typer.Option(..., "--profile", help="Path to a profile YAML"),
    source: str | None = typer.Option(None, "--source", help="Limit to a source id"),
    sample: bool = typer.Option(False, "--sample", help="Use bundled offline fixture"),
    mode: str = typer.Option(
        core.DEFAULT_MODE,
        "--mode",
        help="Operating point: precision|balanced|recall (precision needs an LLM key)",
    ),
    min_score: int | None = typer.Option(
        None, "--min-score", help="Explicit cutoff N (overrides --mode)"
    ),
    limit: int | None = typer.Option(None, "--limit", help="Keep top N"),
    with_benchmarks: bool = typer.Option(
        False, "--with-benchmarks", help="Add ANAC historical benchmark notes"
    ),
    with_documents: bool = typer.Option(
        False, "--with-documents", help="Fetch attachment PDFs into the matcher"
    ),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default: env/home)"),
    json_out: bool = typer.Option(False, "--json", help="JSON output"),
):
    """Rank opportunities for a profile (offline on --sample).

    --mode sets the score cutoff: precision (40), balanced (20, default), recall (0).
    The precision points are meaningful WITH an LLM key — the offline heuristic's
    scores are too coarse to threshold, so keyless runs are recall-oriented.
    """
    store = core.Store(db)
    try:
        company = core.load_profile(profile)
        cutoff = _cutoff_kwargs(mode, min_score)
        ranked = core.run_match(
            company,
            store,
            source_id=source,
            sample=sample,
            limit=limit,
            with_benchmarks=with_benchmarks,
            with_documents=with_documents,
            **cutoff,
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
    mode: str = typer.Option(
        core.DEFAULT_MODE,
        "--mode",
        help="Operating point: precision|balanced|recall (precision needs an LLM key)",
    ),
    min_score: int | None = typer.Option(
        None, "--min-score", help="Explicit cutoff N (overrides --mode)"
    ),
    with_benchmarks: bool = typer.Option(
        False, "--with-benchmarks", help="Add ANAC historical benchmark notes"
    ),
    with_documents: bool = typer.Option(
        False, "--with-documents", help="Fetch attachment PDFs into the matcher"
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Max records to fetch per source (live safety cap)"
    ),
    max_pages: int | None = typer.Option(
        None, "--max-pages", help="Max pages to fetch per source (live safety bound)"
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
    quiet = json_out or rss is not None
    try:
        company = core.load_profile(profile)
        fetch_results, delta = core.run_watch(
            company,
            store,
            source_ids=_source_ids(source),
            sample=sample,
            since=_parse_since(since),
            with_benchmarks=with_benchmarks,
            with_documents=with_documents,
            fetch_limit=limit,
            max_pages=max_pages,
            progress=_progress_sink(quiet),
            **_cutoff_kwargs(mode, min_score),
        )
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()

    # Per-source fetch summary is operational -> stderr (keeps stdout = matches/feed).
    typer.echo(_source_summary(fetch_results), err=True)

    if rss is not None:
        Path(rss).write_text(exporters.to_rss(delta), encoding="utf-8")
        # When --json is also set, stdout must stay pure JSON, so this operational
        # confirmation goes to stderr (like the per-source summary above).
        typer.echo(f"wrote RSS feed: {rss} ({len(delta)} items)", err=json_out)
    if json_out:
        typer.echo(exporters.to_json(delta))
    if not rss and not json_out:
        if not delta:
            typer.echo("No new or amended matches since the last watch.")
        else:
            noun = "match" if len(delta) == 1 else "matches"
            typer.echo(f"{len(delta)} new/amended {noun} for '{company.name}':\n")
            _print_ranked(delta, show_change=True)

    code = core.fetch_exit_code(fetch_results)
    if code:
        raise typer.Exit(code)


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
    profiles_dir: str | None = typer.Option(
        None,
        "--profiles-dir",
        help="Directory of profile YAMLs (default: the bundled example profiles)",
    ),
    source: str | None = typer.Option(
        None, "--source", help="Comma-separated source ids (default: all)"
    ),
    sample: bool = typer.Option(False, "--sample", help="Use bundled offline fixture"),
    mode: str = typer.Option(
        core.DEFAULT_MODE,
        "--mode",
        help="Operating point: precision|balanced|recall (precision needs an LLM key)",
    ),
    min_score: int | None = typer.Option(
        None, "--min-score", help="Explicit cutoff N (overrides --mode)"
    ),
    top: int | None = typer.Option(None, "--top", help="Keep top K per profile"),
    with_benchmarks: bool = typer.Option(
        False, "--with-benchmarks", help="Add ANAC historical benchmark notes"
    ),
    with_documents: bool = typer.Option(
        False, "--with-documents", help="Fetch attachment PDFs into the matcher"
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Max records to fetch per source (live safety cap)"
    ),
    max_pages: int | None = typer.Option(
        None, "--max-pages", help="Max pages to fetch per source (live safety bound)"
    ),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default: env/home)"),
    json_out: bool = typer.Option(False, "--json", help="JSON to stdout"),
    csv_path: str | None = typer.Option(None, "--csv", help="Write CSV to PATH"),
):
    """Run every profile in a directory against the sources and compare results.

    --mode sets the score cutoff (precision|balanced|recall); precision is meaningful
    with an LLM key (the offline heuristic can't threshold cleanly).
    """
    if profiles_dir is not None:
        paths = sorted(Path(profiles_dir).glob("*.yaml"))
        if not paths:
            typer.secho(
                f"No profiles in {profiles_dir!r}", fg=typer.colors.RED, err=True
            )
            raise typer.Exit(1)
        profile_args: list = list(paths)
    else:
        # Installed/checkout default: the bundled example profiles.
        profile_args = list(core.resources.profile_names())

    store = core.Store(db)
    try:
        companies = [core.load_profile(p) for p in profile_args]
        fetch_results, results = core.run_batch(
            companies,
            store,
            source_ids=_source_ids(source),
            sample=sample,
            top=top,
            with_benchmarks=with_benchmarks,
            with_documents=with_documents,
            fetch_limit=limit,
            max_pages=max_pages,
            progress=_progress_sink(json_out or csv_path is not None),
            **_cutoff_kwargs(mode, min_score),
        )
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        store.close()

    # Per-source fetch summary is operational -> stderr (keeps stdout = the table).
    if fetch_results:
        typer.echo(_source_summary(fetch_results), err=True)

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

    code = core.fetch_exit_code(fetch_results)
    if code:
        raise typer.Exit(code)


# --------------------------------------------------------------------------- #
# doctor (diagnostics)
# --------------------------------------------------------------------------- #


def _render_doctor(report) -> str:
    """Human health table + environment section."""
    lines = [
        f"{'SOURCE':14} {'REACH':6} {'NEEDKEY':7} {'KEY?':5} {'STATUS':9} ERROR/NOTE",
        "-" * 72,
    ]
    for s in report.sources:
        reach = "—" if s.reachable is None else ("yes" if s.reachable else "no")
        needk = "yes" if s.needs_key else "no"
        keyq = "—" if s.key_ok is None else ("yes" if s.key_ok else "no")
        note = s.note or "—"
        if len(note) > 30:
            note = note[:29] + "…"
        lines.append(
            f"{_trunc(s.source, 14):14} {reach:6} {needk:7} {keyq:5} "
            f"{s.status:9} {note}"
        )
    e = report.env
    key_state = "present" if e.llm_key_present else "absent"
    extras = " ".join(f"{k}={'yes' if v else 'no'}" for k, v in e.extras.items())
    lines += [
        "",
        "environment:",
        f"  python: {e.python_version}",
        f"  llm: provider={e.llm_provider} key={key_state} "
        f"ready={'yes' if e.llm_ready else 'no'}",
        f"  extras: {extras}",
        f"  db: {'ok' if e.db_ok else f'ERROR: {e.db_error}'}",
        "",
        f"verdict: {'healthy' if report.healthy else 'problems detected'}",
    ]
    return "\n".join(lines)


@app.command()
def doctor(
    source: str | None = typer.Option(
        None, "--source", help="Check a single source id (default: all)"
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the structured DoctorReport"
    ),
    db: str | None = typer.Option(None, "--db", help="SQLite path (default: env/home)"),
):
    """Diagnose source reachability + environment health (one bounded live probe
    per source). Exit 0 if healthy, non-zero (by failure kind) otherwise."""
    try:
        report = core.run_doctor(db=db, source_id=source)
    except Exception as exc:  # noqa: BLE001 — clean operational message, no traceback
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    if json_out:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(_render_doctor(report))

    if report.exit_code:
        raise typer.Exit(report.exit_code)


# --------------------------------------------------------------------------- #
# eval (matching-quality evaluation)
# --------------------------------------------------------------------------- #


def _render_eval(report) -> str:
    head = (
        f"{'PROFILE':28} {'P@5':>5} {'P@10':>5} {'RECALL':>6} {'FPR':>5} "
        f"{'RET':>4} {'POOL':>4}"
    )
    lines = [
        f"corpus: {report.corpus_size} opportunities | "
        f"profiles: {len(report.gold_profiles)} | eval_now: {report.eval_now}",
        f"labels: {report.note}",
    ]

    def row(label, m):
        return (
            f"{_trunc(label, 28):28} {m.precision_at_5:>5.2f} "
            f"{m.precision_at_10:>5.2f} {m.recall:>6.2f} "
            f"{m.false_positive_rate:>5.2f} {m.returned:>4} {m.pool:>4}"
        )

    for method in report.methods:
        lines += ["", f"method: {method.method}", head, "-" * len(head)]
        for p in method.profiles:
            lines.append(row(p.profile, p.metrics))
        lines.append("-" * len(head))
        lines.append(row("AGGREGATE (macro avg)", method.aggregate))

    if report.attribution_k is not None:
        lines += _render_attribution(report)
    if report.gate_drops:
        lines += _render_gate_drops(report)
    if report.thresholds:
        lines += _render_sweep(report)
    if report.full_text:
        lines += _render_full_text(report)
    if report.embeddings is not None:
        lines += _render_embeddings(report.embeddings)
    return "\n".join(lines)


def _render_embeddings(emb) -> list[str]:
    """WITH/WITHOUT semantic-prefilter table (heuristic, offline). DROP = Stage-1
    prefilter drops — the recall lever; should fall vs baseline."""
    lines = [
        "",
        "== embeddings semantic prefilter — WITH vs WITHOUT (heuristic, offline)",
    ]
    if not emb.available:
        lines.append(
            "   backend unavailable — install + enable: uv sync --extra embeddings "
            "(BANDIRADAR_EMBEDDINGS!=none)"
        )
        return lines
    lines.append(f"   model: {emb.model_id}")
    head = (
        f"{'CONFIG':22} {'P@5':>5} {'P@10':>5} {'RECALL':>6} {'FPR':>5} "
        f"{'RET':>4} {'DROP':>4}"
    )
    lines += [head, "-" * len(head)]
    for run in emb.runs:
        m = run.aggregate
        drop = run.attribution.prefilter_drop if run.attribution else 0
        lines.append(
            f"{_trunc(run.label, 22):22} {m.precision_at_5:>5.2f} "
            f"{m.precision_at_10:>5.2f} {m.recall:>6.2f} "
            f"{m.false_positive_rate:>5.2f} {m.returned:>4} {drop:>4}"
        )
    return lines


def _render_attribution(report) -> list[str]:
    """Where relevant-for-recall items end up: Stage-1 prefilter vs Stage-2 ranking."""
    k = report.attribution_k
    head = f"{'PROFILE':28} {'WANT':>4} {'DROP':>4} {'BELOW':>5} {'TOP':>4}"
    lines = [
        "",
        f"== recall attribution (k={k}) — WHY relevant-for-recall items are missed",
        "   DROP = dropped by Stage-1 prefilter (→ embeddings); "
        f"BELOW = returned but ranked ≥{k} (→ reranking); TOP = surfaced in top-{k}",
    ]

    def row(label, a):
        return (
            f"{_trunc(label, 28):28} {a.wanted:>4} {a.prefilter_drop:>4} "
            f"{a.below_k:>5} {a.in_top_k:>4}"
        )

    for method in report.methods:
        lines += ["", f"method: {method.method}", head, "-" * len(head)]
        for p in method.profiles:
            if p.attribution is not None:
                lines.append(row(p.profile, p.attribution))
        if method.attribution is not None:
            lines.append("-" * len(head))
            lines.append(row("AGGREGATE (totals)", method.attribution))
    return lines


def _render_gate_drops(report) -> list[str]:
    """Which Stage-1 gate killed each prefilter-dropped relevant item — over-strict
    gate (tunable) vs real ceiling."""
    from collections import Counter

    tally = Counter(d.gate for d in report.gate_drops)
    tally_str = " ".join(f"{g}={n}" for g, n in tally.most_common())
    head = f"{'PROFILE':22} {'OPPORTUNITY':26} {'LABEL':10} {'GATE':16} REASON"
    lines = [
        "",
        "== gate attribution — which Stage-1 gate dropped each relevant item",
        f"   tally: {tally_str}",
        head,
        "-" * len(head),
    ]
    for d in report.gate_drops:
        lines.append(
            f"{_trunc(d.profile, 22):22} {_trunc(d.opportunity_id, 26):26} "
            f"{d.label:10} {d.gate:16} {d.reason}"
        )
    return lines


def _render_sweep(report) -> list[str]:
    """The precision/recall/FPR curve across min_score cutoffs (aggregate)."""
    head = f"{'THRESH':>6} {'P@5':>5} {'P@10':>5} {'RECALL':>6} {'FPR':>5} {'RET':>4}"
    lines = ["", "== min_score sweep — precision/recall/FPR vs cutoff (aggregate)"]
    for method in report.methods:
        lines += ["", f"method: {method.method}", head, "-" * len(head)]
        for point in method.sweep:
            m = point.aggregate
            lines.append(
                f"{point.threshold:>6} {m.precision_at_5:>5.2f} "
                f"{m.precision_at_10:>5.2f} {m.recall:>6.2f} "
                f"{m.false_positive_rate:>5.2f} {m.returned:>4}"
            )
    return lines


def _render_full_text(report) -> list[str]:
    """Before/after: capped brief vs full requirements text (aggregate)."""
    head = f"{'METHOD':28} {'METRIC':>8} {'BRIEF':>6} {'FULL':>6} {'Δ':>7}"
    lines = [
        "",
        "== full-text experiment — capped brief vs FULL requirements text (aggregate)",
        "   (the heuristic already reads full text, so its delta is ~0 by design)",
        head,
        "-" * len(head),
    ]
    metrics = [
        ("P@5", "precision_at_5"),
        ("P@10", "precision_at_10"),
        ("RECALL", "recall"),
        ("FPR", "false_positive_rate"),
    ]
    for ft in report.full_text:
        for i, (label, attr) in enumerate(metrics):
            b = getattr(ft.brief, attr)
            f = getattr(ft.full, attr)
            name = _trunc(ft.method, 28) if i == 0 else ""
            lines.append(f"{name:28} {label:>8} {b:>6.2f} {f:>6.2f} {f - b:>+7.2f}")
    return lines


@app.command(name="eval")
def eval_cmd(
    with_benchmarks: bool = typer.Option(
        False, "--with-benchmarks", help="Add ANAC historical benchmark enrichment"
    ),
    with_documents: bool = typer.Option(
        False, "--with-documents", help="Fold attachment-PDF text into matching"
    ),
    diagnostics: bool = typer.Option(
        False,
        "--diagnostics",
        "-d",
        help="Add recall attribution (Stage 1 vs 2) + min_score threshold sweep",
    ),
    full_text: bool = typer.Option(
        False,
        "--full-text",
        help="Experiment: re-score with FULL requirements text, report delta vs brief",
    ),
    embeddings: bool = typer.Option(
        False,
        "--embeddings",
        help="Experiment: hybrid semantic prefilter WITH vs WITHOUT (needs the extra)",
    ),
    rerank: bool = typer.Option(
        False,
        "--rerank",
        help="Add a LISTWISE LLM method (one comparative call/profile) if a key is set",
    ),
    db: str | None = typer.Option(
        None, "--db", help="SQLite path (default: in-memory throwaway)"
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit the structured report"),
):
    """Evaluate matching quality over the shipped labelled corpus (offline).

    Always reports the heuristic matcher; if an LLM provider+key is configured, also
    the LLM matcher on the same gold set. ``--diagnostics`` adds (free) recall
    attribution + a min_score sweep; ``--full-text`` runs the controlled full-text
    experiment. No live fetch happens here.
    """
    from bandiradar import evaluation

    try:
        report = evaluation.run_eval(
            db=db,
            with_benchmarks=with_benchmarks,
            with_documents=with_documents,
            diagnostics=diagnostics,
            full_text=full_text,
            embeddings=embeddings,
            rerank=rerank,
        )
    except Exception as exc:  # noqa: BLE001 — clean operational message, no traceback
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    if json_out:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(_render_eval(report))


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
