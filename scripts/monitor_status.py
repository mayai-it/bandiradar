"""Generate ``STATUS.md`` for the daily live monitor (offline, pure composition).

Reads ONLY local state — never the network:
  * the SQLite DB (``runs`` table -> per-source esito/conteggi; ``crawl_recipes`` +
    ``crawl_golden`` -> the self-healing crawl audit);
  * the per-profile feed JSON the ``watch`` runs wrote (-> new-match counts);
  * an optional ``doctor --json`` report (-> the LIVE crawl-health drift signal).

It composes those into a Markdown status page and decides the run verdict: the
job FAILS (exit 1) only if EVERY fetched source failed; partial failures surface
as warnings in the page. No business logic from the engine is duplicated here —
this is observability over what the run already persisted.

Run (in the workflow, after the watch runs + doctor):
    uv run python scripts/monitor_status.py \
        --db state/bandiradar.db --feeds state/feeds --doctor state/doctor.json \
        --profiles mayai,manifattura,... --out state/STATUS.md --run-started <iso>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# Data shapes (plain, so the render layer is pure and trivially testable)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceRow:
    """The latest ``runs`` row for one source — the persisted SourceResult."""

    source: str
    status: str  # ok | empty | partial | failed | running
    fetched: int
    new: int
    amended: int
    error: str | None
    error_kind: str | None

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def warned(self) -> bool:
        return self.status in ("partial", "failed")


@dataclass(frozen=True)
class RecipeState:
    """Self-healing crawl state for one crawl-bearing source."""

    source: str
    state: str  # ok | drift | healed | flagged | unknown
    detail: str | None = None


# --------------------------------------------------------------------------- #
# DB readers (pure SQL, read-only)
# --------------------------------------------------------------------------- #


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def latest_runs(conn: sqlite3.Connection) -> dict[str, SourceRow]:
    """The most recent finished/started ``runs`` row per source (by id == recency)."""
    rows = conn.execute(
        'SELECT source, status, fetched, "new" AS new, amended, error, error_kind '
        "FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY source) "
        "ORDER BY source"
    ).fetchall()
    out: dict[str, SourceRow] = {}
    for r in rows:
        if r["source"] is None:
            continue
        out[r["source"]] = SourceRow(
            source=r["source"],
            status=r["status"] or "unknown",
            fetched=r["fetched"] or 0,
            new=r["new"] or 0,
            amended=r["amended"] or 0,
            error=r["error"],
            error_kind=r["error_kind"],
        )
    return out


def recipe_audits(conn: sqlite3.Connection) -> dict[str, dict]:
    """Adopted crawl-recipe overrides keyed by source (the heal audit trail)."""
    rows = conn.execute(
        "SELECT source_id, adopted_at, reason, validated_by FROM crawl_recipes"
    ).fetchall()
    return {r["source_id"]: dict(r) for r in rows}


def golden_sources(conn: sqlite3.Connection) -> set[str]:
    """Sources that have snapshotted a last-good golden (their crawl ran healthy)."""
    rows = conn.execute("SELECT source_id FROM crawl_golden").fetchall()
    return {r["source_id"] for r in rows}


def trust_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Per-source trust-verdict counts over stored LLM extractions (trust spine).

    Mirrors ``storage.Store.trust_counts`` (read straight off the stored JSON);
    sources without assessed extractions (structured adapters) are absent.
    """
    rows = conn.execute(
        "SELECT source, json_extract(data, '$.trust_verdict') AS verdict, "
        "COUNT(*) AS n FROM opportunities "
        "WHERE json_extract(data, '$.trust_verdict') IS NOT NULL "
        "GROUP BY source, verdict"
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["source"], {})[r["verdict"]] = r["n"]
    return out


# --------------------------------------------------------------------------- #
# Pure derivations
# --------------------------------------------------------------------------- #


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def derive_recipe_state(
    *,
    crawl_health: str | None,
    audit: dict | None,
    healed_this_run: bool,
    llm_active: bool,
    has_golden: bool = False,
) -> str:
    """Map persisted + live crawl signals to one of ok|drift|healed|flagged|unknown.

    Precedence (the LIVE probe wins over the persisted fallback):
      * a recipe ADOPTED during this run (audit, drift-heal) -> ``healed``;
      * a live drifted crawl (``degraded``/``broken``) -> ``flagged`` when the LLM is
        ACTIVE (the healer ran but could NOT auto-adopt -> needs a human) else
        ``drift`` (no live LLM: drift is only DETECTED, never healed);
      * a healthy live crawl -> ``ok``;
      * no live probe -> fall back to the DB: an override -> ``healed``, a golden
        snapshot (a past healthy crawl) -> ``ok``, otherwise -> ``unknown``.
    """
    if healed_this_run:
        return "healed"
    if crawl_health in ("degraded", "broken"):
        return "flagged" if llm_active else "drift"
    if crawl_health == "ok":
        return "ok"
    # No live probe this run: fall back to what the DB knows.
    if audit is not None:
        return "healed"
    if has_golden:
        return "ok"
    return "unknown"


def recipe_states(
    *,
    audits: dict[str, dict],
    goldens: set[str],
    crawl_health: dict[str, str | None],
    run_started: datetime | None,
    llm_active: bool,
) -> list[RecipeState]:
    """One RecipeState per crawl-bearing source (golden/override/live-probe union)."""
    sources = sorted(set(audits) | goldens | set(crawl_health))
    states: list[RecipeState] = []
    for src in sources:
        audit = audits.get(src)
        adopted = _parse_iso(audit["adopted_at"]) if audit else None
        healed_this_run = bool(
            adopted is not None and run_started is not None and adopted >= run_started
        )
        state = derive_recipe_state(
            crawl_health=crawl_health.get(src),
            audit=audit,
            healed_this_run=healed_this_run,
            llm_active=llm_active,
            has_golden=src in goldens,
        )
        detail = None
        if audit and state == "healed":
            detail = (
                f"adopted {audit['adopted_at']} "
                f"(reason={audit['reason']}, validated_by={audit['validated_by']})"
            )
        states.append(RecipeState(source=src, state=state, detail=detail))
    return states


def count_matches(feed_json: Path) -> int | None:
    """Number of matches in a watch feed JSON (the documented array). None if absent."""
    if not feed_json.exists():
        return None
    text = feed_json.read_text(encoding="utf-8").strip()
    if not text:
        return 0
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):  # defensive: tolerate a wrapped shape
        for key in ("matches", "items", "results"):
            if isinstance(data.get(key), list):
                return len(data[key])
    return None


def scoring_stats(feeds_dir: Path, profiles: list[str]) -> dict[str, dict | None]:
    """Per-profile ``{scored, deferred}`` from ``<profile>.stats.json``.

    The sidecar is written by ``watch --stats-out`` ONLY when a run completes, so it
    doubles as a completion marker: ``None`` means that profile did NOT finish (the
    run was truncated before reaching it, or it was killed mid-scoring)."""
    out: dict[str, dict | None] = {}
    for prof in profiles:
        p = feeds_dir / f"{prof}.stats.json"
        if not p.exists():
            out[prof] = None
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out[prof] = data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            out[prof] = None
    return out


def crawl_health_from_doctor(doctor_json: Path | None) -> dict[str, str | None]:
    """Map source -> live crawl_health from a ``doctor --json`` report (keyless)."""
    if doctor_json is None or not doctor_json.exists():
        return {}
    try:
        report = json.loads(doctor_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[str, str | None] = {}
    for s in report.get("sources", []):
        ch = s.get("crawl_health")
        if ch is not None:
            out[s["source"]] = ch
    return out


def all_failed(runs: dict[str, SourceRow]) -> bool:
    """True only when there ARE sources and EVERY one failed (the fail verdict)."""
    return bool(runs) and all(r.failed for r in runs.values())


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #

_STATUS_EMOJI = {
    "ok": "✅",
    "empty": "⚪",
    "partial": "⚠️",
    "failed": "❌",
    "running": "⏳",
}
_RECIPE_EMOJI = {
    "ok": "✅",
    "healed": "🩹",
    "drift": "⚠️",
    "flagged": "🚩",
    "unknown": "❔",
}


def render_status(
    *,
    run_date: str,
    runs: dict[str, SourceRow],
    states: list[RecipeState],
    match_counts: dict[str, int | None],
    stats: dict[str, dict | None],
    llm_active: bool,
    trust: dict[str, dict[str, int]] | None = None,
) -> str:
    """Compose the Markdown page. Pure: same inputs -> same bytes."""
    expected = sorted(stats)
    completed = [p for p in expected if stats.get(p) is not None]
    truncated = bool(expected) and len(completed) < len(expected)
    scored = sum((stats[p] or {}).get("scored", 0) for p in completed)
    deferred = sum((stats[p] or {}).get("deferred", 0) for p in completed)

    lines: list[str] = []
    lines.append("# BandiRadar — live monitor status")
    lines.append("")
    # Reflects the REAL LLM client (verified by the workflow), not just key presence.
    mode = "LLM scoring + healer ON" if llm_active else "keyless (recall mode)"
    lines.append(f"- **Run:** {run_date}")
    lines.append(f"- **Mode:** {mode}")
    if truncated:
        lines.append(
            f"- **⚠️ Run truncated:** {len(completed)}/{len(expected)} profiles "
            "completed — figures below cover only the completed profiles; the rest "
            "were cut short (e.g. step timeout) and are NOT republished as fresh."
        )
    if scored or deferred:
        lines.append(
            f"- **LLM scoring:** {scored} scored, {deferred} deferred to a later "
            "run (per-run budget; the score cache amortizes them)."
        )
    failed = sum(1 for r in runs.values() if r.failed)
    partial = sum(1 for r in runs.values() if r.status == "partial")
    if all_failed(runs):
        lines.append("- **Verdict:** ❌ ALL sources failed — run marked failed.")
    elif failed or partial:
        lines.append(
            f"- **Verdict:** ⚠️ partial — {failed} failed, {partial} degraded "
            f"(others OK; see below)."
        )
    else:
        lines.append("- **Verdict:** ✅ all sources healthy.")
    lines.append("")

    lines.append("## Sources")
    lines.append("")
    lines.append("| Source | Status | Fetched | New | Amended | Note |")
    lines.append("|---|---|---:|---:|---:|---|")
    for src in sorted(runs):
        r = runs[src]
        emoji = _STATUS_EMOJI.get(r.status, "")
        note = ""
        if r.error:
            kind = f" [{r.error_kind}]" if r.error_kind else ""
            note = f"{r.error}{kind}"
        lines.append(
            f"| `{src}` | {emoji} {r.status} | {r.fetched} | {r.new} | "
            f"{r.amended} | {note} |"
        )
    if not runs:
        lines.append("| _no source runs recorded_ | | | | | |")
    lines.append("")

    if trust:
        lines.append("## Extraction trust (LLM sources)")
        lines.append("")
        lines.append("| Source | OK | Suspect | Quarantined |")
        lines.append("|---|---:|---:|---:|")
        for src in sorted(trust):
            c = trust[src]
            quarantined = c.get("quarantine", 0)
            q_cell = f"🚧 {quarantined}" if quarantined else "0"
            lines.append(
                f"| `{src}` | {c.get('ok', 0)} | {c.get('suspect', 0)} | {q_cell} |"
            )
        lines.append("")
        lines.append(
            "> Deterministic verdicts over each LLM extraction (`bandiradar.trust`): "
            "`quarantine` = a hard check failed (e.g. extracted deadline not in the "
            "page) — kept in the DB for audit but EXCLUDED from matching; "
            "`suspect` = low confidence, still matched. "
            "Inspect: `bandiradar trust list`."
        )
        lines.append("")

    lines.append("## New matches per profile")
    lines.append("")
    lines.append("| Profile | New/amended matches |")
    lines.append("|---|---:|")
    for prof in sorted(match_counts):
        if stats.get(prof) is None:  # did not complete -> don't show a stale figure
            cell = "⚠️ incomplete"
        else:
            n = match_counts[prof]
            cell = "n/a" if n is None else str(n)
        lines.append(f"| `{prof}` | {cell} |")
    if not match_counts:
        lines.append("| _no profiles_ | |")
    lines.append("")

    lines.append("## Self-healing crawl")
    lines.append("")
    if states:
        lines.append("| Source | Recipe state | Detail |")
        lines.append("|---|---|---|")
        for st in states:
            emoji = _RECIPE_EMOJI.get(st.state, "")
            lines.append(f"| `{st.source}` | {emoji} {st.state} | {st.detail or ''} |")
    else:
        lines.append("_No crawl-bearing source has run yet (no golden/recipe/probe)._")
    lines.append("")
    lines.append(
        "> `drift` = listing changed, no key to heal · `flagged` = drift + key but "
        "the re-derived recipe did NOT reproduce the golden (needs a human) · "
        "`healed` = a re-derived recipe was auto-adopted (reproduced the golden "
        "exactly) this run."
    )
    lines.append("")
    lines.append(
        "_Generated by `scripts/monitor_status.py` — offline, from the run's DB + "
        "feeds. No network._"
    )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def build_status(
    *,
    db_path: str,
    feeds_dir: Path,
    profiles: list[str],
    doctor_json: Path | None,
    run_date: str,
    run_started: datetime | None,
    llm_active: bool,
) -> tuple[str, bool]:
    """Read all local state and render STATUS.md. Returns ``(markdown, all_failed)``."""
    conn = _connect(db_path)
    try:
        runs = latest_runs(conn)
        audits = recipe_audits(conn)
        goldens = golden_sources(conn)
        trust = trust_counts(conn)
    finally:
        conn.close()
    health = crawl_health_from_doctor(doctor_json)
    states = recipe_states(
        audits=audits,
        goldens=goldens,
        crawl_health=health,
        run_started=run_started,
        llm_active=llm_active,
    )
    match_counts = {
        prof: count_matches(feeds_dir / f"{prof}.json") for prof in profiles
    }
    stats = scoring_stats(feeds_dir, profiles)
    md = render_status(
        run_date=run_date,
        runs=runs,
        states=states,
        match_counts=match_counts,
        stats=stats,
        llm_active=llm_active,
        trust=trust,
    )
    return md, all_failed(runs)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate the live-monitor STATUS.md.")
    p.add_argument("--db", required=True, help="SQLite DB path")
    p.add_argument("--feeds", required=True, help="Directory holding <profile>.json")
    p.add_argument("--profiles", required=True, help="Comma-separated profile names")
    p.add_argument("--doctor", default=None, help="Optional doctor --json report path")
    p.add_argument("--out", required=True, help="STATUS.md output path")
    p.add_argument("--run-date", default=None, help="Human run date (default: now UTC)")
    p.add_argument(
        "--run-started",
        default=None,
        help="ISO timestamp of run start (gates 'healed this run'); default: now UTC",
    )
    p.add_argument(
        "--llm-active",
        action="store_true",
        help="The LLM client is REALLY usable (provider+key set AND SDK importable, "
        "as verified by the workflow) — scoring + healer active. Drives the Mode "
        "line and flagged-vs-drift. Absent => keyless/recall.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    now = datetime.now(UTC)
    run_started = _parse_iso(args.run_started) or now
    run_date = args.run_date or now.strftime("%Y-%m-%d %H:%M UTC")
    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    md, failed = build_status(
        db_path=args.db,
        feeds_dir=Path(args.feeds),
        profiles=profiles,
        doctor_json=Path(args.doctor) if args.doctor else None,
        run_date=run_date,
        run_started=run_started,
        llm_active=args.llm_active,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    # Exit 1 ONLY when every source failed; partial failures stay exit 0 (warned
    # in the page) so the workflow still commits the status and does not abort.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
