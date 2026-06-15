"""SQLite store: persistence + dedupe + change-detection (ARCHITECTURE.md §8).

Stdlib ``sqlite3``, no ORM. Holds opportunities, raw docs, matches, and run
bookkeeping, and backs the Stage-2 relevance cache (:class:`SqliteScoreCache`).
Change detection is driven by ``content_hash`` (§8): a changed hash bumps the
``version`` and stamps ``updated_at``, making the row a re-notifiable *rettifica*
— surfaced by ``list_new(since)`` / the watch delta. This module is persistence
only — orchestration lives in core.

Lifecycle ``status`` (open/closing_soon/closed) is DERIVED from ``deadline`` + the
current time on every read (see :func:`_load_opportunity`), never trusted from
storage — so it is always current and the "changed" signal stays separate from it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from bandiradar.models import Match, Opportunity, RawDoc, default_status

UpsertResult = Literal["new", "unchanged", "amended"]


def _load_opportunity(data: str, now: datetime) -> Opportunity:
    """Deserialize an Opportunity and recompute its lifecycle ``status`` at ``now``.

    Status is DERIVED from ``deadline`` + ``now`` on every read (never trusted from
    storage), so an item never reads back "open" past its deadline. This also
    tolerates legacy rows that stored ``status="amended"`` (no longer a valid
    status) by overwriting status before validation.
    """
    payload = json.loads(data)
    raw_deadline = payload.get("deadline")
    deadline = datetime.fromisoformat(raw_deadline) if raw_deadline else None
    payload["status"] = default_status(deadline, now)
    return Opportunity.model_validate(payload)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    version      INTEGER NOT NULL,
    status       TEXT NOT NULL,
    deadline     TEXT,
    updated_at   TEXT,
    inserted_at  TEXT NOT NULL,
    data         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_docs (
    id         TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    opportunity_id   TEXT NOT NULL,
    profile_version  TEXT NOT NULL,
    opportunity_hash TEXT NOT NULL,
    cache_key        TEXT NOT NULL DEFAULT '',
    score            INTEGER NOT NULL,
    data             TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, profile_version)
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    fetched         INTEGER,
    "new"           INTEGER,
    amended         INTEGER,
    mapped          INTEGER,
    skipped_invalid INTEGER,
    duration_s      REAL,
    status          TEXT,   -- 'running' | ok | partial | failed | empty
    error           TEXT,   -- clean operational error (partial/failed)
    error_kind      TEXT    -- rate_limited | unavailable | invalid | unknown
);

CREATE TABLE IF NOT EXISTS watch_state (
    profile_version TEXT PRIMARY KEY,
    last_watch      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    url_hash TEXT PRIMARY KEY,   -- sha256(url)
    url      TEXT NOT NULL,
    text     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extractions (
    url_hash TEXT PRIMARY KEY,   -- sha256(detail url)
    url      TEXT NOT NULL,
    data     TEXT NOT NULL,      -- JSON of the LLM-extracted bando fields
    trust    TEXT                -- JSON TrustReport over that extraction (nullable)
);

CREATE TABLE IF NOT EXISTS embeddings (
    content_hash TEXT NOT NULL,  -- Opportunity.content_hash (self-invalidates on amend)
    model_id     TEXT NOT NULL,  -- embedder identity (e.g. fastembed:<model>)
    vector       TEXT NOT NULL,  -- JSON array of floats
    PRIMARY KEY (content_hash, model_id)
);

CREATE TABLE IF NOT EXISTS crawl_recipes (
    source_id    TEXT PRIMARY KEY,  -- per-source CrawlRecipe override (config)
    recipe       TEXT NOT NULL,     -- JSON of CrawlRecipe
    adopted_at   TEXT NOT NULL,
    reason       TEXT,              -- why adopted (e.g. "drift-heal")
    validated_by TEXT               -- which guard passed (e.g. "golden-exact")
);

CREATE TABLE IF NOT EXISTS crawl_golden (
    source_id TEXT PRIMARY KEY,  -- last KNOWN-GOOD refs (the drift-heal golden)
    refs      TEXT NOT NULL,     -- JSON list of [post_id, detail_url, title]
    saved_at  TEXT NOT NULL
);
"""

# Columns added AFTER a table's original release. ``_migrate`` introspects each
# table and ALTERs in any that are missing, so an old-schema DB upgrades cleanly
# (a fresh DB already has them from _SCHEMA, so the ALTERs are skipped). Keep each
# declaration ALTER-compatible: a NOT NULL column must carry a DEFAULT.
_EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "matches": {"cache_key": "TEXT NOT NULL DEFAULT ''"},
    "runs": {
        "status": "TEXT",
        "error": "TEXT",
        "error_kind": "TEXT",
        "mapped": "INTEGER",
        "skipped_invalid": "INTEGER",
        "duration_s": "REAL",
    },
    # 0.12.0 trust spine: the TrustReport persisted beside its extraction. Old
    # rows get NULL -> the scraper backfills the report on its next cache hit.
    "extractions": {"trust": "TEXT"},
}

# Indexes that reference possibly-migrated columns. Created ONLY AFTER the columns
# are guaranteed to exist (so they never run against an un-upgraded table).
_MIGRATION_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_matches_cache_key ON matches (cache_key)",
)


def _default_db_path() -> str:
    env = os.environ.get("BANDIRADAR_DB")
    if env:
        return env
    return str(Path.home() / ".bandiradar" / "bandiradar.db")


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return now if now.tzinfo is not None else now.replace(tzinfo=UTC)


def _to_text(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt is not None else None


class Store:
    """SQLite-backed persistence with dedupe and change detection."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path if db_path is not None else _default_db_path()
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # Order matters for the upgrade path (see _migrate): (a) create base tables,
        # (b) add any missing columns, (c) ONLY THEN create indexes that reference
        # them. Nothing here may touch a column before _migrate guarantees it.
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring an existing DB up to the current schema (idempotent).

        Introspects each table with ``PRAGMA table_info`` and ALTERs in every
        expected-but-missing column, then (re)creates indexes that reference those
        columns. Safe to run on a fresh DB (all columns already present -> no-op)
        and on any older DB (no schema-version assumption).
        """
        for table, columns in _EXPECTED_COLUMNS.items():
            existing = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns.items():
                if name not in existing:
                    # e.g. old `matches` rows get cache_key='' -> they simply miss
                    # the richer score-cache key and are re-scored, never a false hit.
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        for ddl in _MIGRATION_INDEXES:
            self.conn.execute(ddl)

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------------- raw docs
    def save_raw_doc(self, raw: RawDoc) -> None:
        """Upsert a raw payload by id."""
        self.conn.execute(
            "INSERT INTO raw_docs (id, source, fetched_at, payload) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "source=excluded.source, fetched_at=excluded.fetched_at, "
            "payload=excluded.payload",
            (raw.id, raw.source, _to_text(raw.fetched_at), json.dumps(raw.payload)),
        )
        self.conn.commit()

    def get_raw_doc(self, raw_id: str) -> RawDoc | None:
        row = self.conn.execute(
            "SELECT id, source, fetched_at, payload FROM raw_docs WHERE id=?",
            (raw_id,),
        ).fetchone()
        if row is None:
            return None
        return RawDoc(
            id=row["id"],
            source=row["source"],
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            payload=json.loads(row["payload"]),
        )

    # ------------------------------------------------------------ opportunities
    def upsert_opportunity(
        self, opp: Opportunity, now: datetime | None = None
    ) -> UpsertResult:
        """Insert, no-op, or amend by content_hash (§8 change detection)."""
        moment = _now(now)
        row = self.conn.execute(
            "SELECT content_hash, version FROM opportunities WHERE id=?",
            (opp.id,),
        ).fetchone()

        if row is None:
            self._write_opportunity(opp, inserted_at=moment, updated_at=None)
            return "new"

        if row["content_hash"] == opp.content_hash:
            return "unchanged"

        # Changed (rettifica): bump version + stamp updated_at — that IS the change
        # signal (surfaced by list_new / the watch delta). ``status`` stays purely
        # lifecycle; it is NOT overwritten with a sticky "amended" anymore.
        amended = opp.model_copy(
            update={
                "version": row["version"] + 1,
                "updated_at": moment,
            }
        )
        self._write_opportunity(amended, inserted_at=None, updated_at=moment)
        return "amended"

    def _write_opportunity(
        self,
        opp: Opportunity,
        inserted_at: datetime | None,
        updated_at: datetime | None,
    ) -> None:
        if inserted_at is not None:
            # New row.
            self.conn.execute(
                "INSERT INTO opportunities "
                "(id, source, content_hash, version, status, deadline, "
                " updated_at, inserted_at, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    opp.id,
                    opp.source,
                    opp.content_hash,
                    opp.version,
                    opp.status,
                    _to_text(opp.deadline),
                    _to_text(updated_at),
                    _to_text(inserted_at),
                    opp.model_dump_json(),
                ),
            )
        else:
            # Existing row being amended; inserted_at is left untouched.
            self.conn.execute(
                "UPDATE opportunities SET "
                "source=?, content_hash=?, version=?, status=?, deadline=?, "
                "updated_at=?, data=? WHERE id=?",
                (
                    opp.source,
                    opp.content_hash,
                    opp.version,
                    opp.status,
                    _to_text(opp.deadline),
                    _to_text(updated_at),
                    opp.model_dump_json(),
                    opp.id,
                ),
            )
        self.conn.commit()

    def get_opportunity(
        self, opp_id: str, now: datetime | None = None
    ) -> Opportunity | None:
        """Read one opportunity with its lifecycle ``status`` recomputed at ``now``."""
        row = self.conn.execute(
            "SELECT data FROM opportunities WHERE id=?", (opp_id,)
        ).fetchone()
        return _load_opportunity(row["data"], _now(now)) if row else None

    def list_opportunities(
        self,
        status: str | None = None,
        source: str | None = None,
        now: datetime | None = None,
    ) -> list[Opportunity]:
        """List opportunities; ``status`` is recomputed at ``now`` on the way out.

        The ``status`` filter applies to the RECOMPUTED lifecycle status (not the
        stored column), so filtering stays consistent with what callers read.
        """
        moment = _now(now)
        where = " WHERE source=?" if source is not None else ""
        params = [source] if source is not None else []
        # `where` is fixed clause text (no user input); values are bound via `?`.
        query = f"SELECT data FROM opportunities{where} ORDER BY id"  # nosec B608
        rows = self.conn.execute(query, params).fetchall()
        opps = [_load_opportunity(r["data"], moment) for r in rows]
        if status is not None:
            opps = [o for o in opps if o.status == status]
        return opps

    def list_new(
        self, since: datetime | None, now: datetime | None = None
    ) -> list[Opportunity]:
        """Opportunities CHANGED after ``since`` (all when ``since`` is None).

        "Changed" = last store mutation (updated_at if a notice was amended, else
        inserted_at) — this is the change signal, kept separate from lifecycle
        ``status`` (which is recomputed at ``now`` for display).
        """
        moment = _now(now)
        rows = self.conn.execute(
            "SELECT data, inserted_at, updated_at FROM opportunities"
        ).fetchall()
        threshold = _now(since) if since is not None else None
        out: list[tuple[datetime, Opportunity]] = []
        for row in rows:
            changed_text = row["updated_at"] or row["inserted_at"]
            changed_at = datetime.fromisoformat(changed_text)
            if threshold is None or changed_at > threshold:
                out.append((changed_at, _load_opportunity(row["data"], moment)))
        out.sort(key=lambda pair: pair[0])
        return [opp for _, opp in out]

    # ----------------------------------------------------------------- matches
    def save_match(self, match: Match, cache_key: str = "") -> None:
        """Upsert a match by (opportunity_id, profile_version).

        ``cache_key`` is the full relevance-cache fingerprint (backend + document
        state + content); :class:`SqliteScoreCache` looks rows up by it so a
        differently-scored input never reuses this row.
        """
        self.conn.execute(
            "INSERT INTO matches "
            "(opportunity_id, profile_version, opportunity_hash, cache_key, score, "
            " data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(opportunity_id, profile_version) DO UPDATE SET "
            "opportunity_hash=excluded.opportunity_hash, cache_key=excluded.cache_key, "
            "score=excluded.score, data=excluded.data, created_at=excluded.created_at",
            (
                match.opportunity_id,
                match.profile_version,
                match.opportunity_hash,
                cache_key,
                match.score,
                match.model_dump_json(),
                _to_text(_now(None)),
            ),
        )
        self.conn.commit()

    def get_match(self, opportunity_id: str, profile_version: str) -> Match | None:
        row = self.conn.execute(
            "SELECT data FROM matches WHERE opportunity_id=? AND profile_version=?",
            (opportunity_id, profile_version),
        ).fetchone()
        return Match.model_validate_json(row["data"]) if row else None

    def list_matches(
        self, profile_version: str, min_score: int = 0, limit: int | None = None
    ) -> list[Match]:
        """Persisted matches for a profile_version, score descending."""
        rows = self.conn.execute(
            "SELECT data FROM matches WHERE profile_version=? AND score>=? "
            "ORDER BY score DESC",
            (profile_version, min_score),
        ).fetchall()
        matches = [Match.model_validate_json(r["data"]) for r in rows]
        return matches[:limit] if limit is not None else matches

    # -------------------------------------------------------------------- runs
    def start_run(self, source: str | None, started_at: datetime | None = None) -> int:
        cur = self.conn.execute(
            'INSERT INTO runs (source, started_at, fetched, "new", amended, status) '
            "VALUES (?, ?, 0, 0, 0, 'running')",
            (source, _to_text(_now(started_at))),
        )
        self.conn.commit()
        assert cur.lastrowid is not None  # guaranteed right after an INSERT
        return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        fetched: int,
        new: int,
        amended: int,
        finished_at: datetime | None = None,
        status: str = "ok",
        error: str | None = None,
        error_kind: str | None = None,
        mapped: int = 0,
        skipped_invalid: int = 0,
        duration_s: float | None = None,
    ) -> None:
        """Finalize a run row with its outcome + counts (one row per source/run)."""
        self.conn.execute(
            'UPDATE runs SET finished_at=?, fetched=?, "new"=?, amended=?, '
            "mapped=?, skipped_invalid=?, duration_s=?, status=?, error=?, "
            "error_kind=? WHERE id=?",
            (
                _to_text(_now(finished_at)),
                fetched,
                new,
                amended,
                mapped,
                skipped_invalid,
                duration_s,
                status,
                error,
                error_kind,
                run_id,
            ),
        )
        self.conn.commit()

    def get_run(self, run_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- watch state
    def get_watch_marker(self, profile_version: str) -> datetime | None:
        """The last watch timestamp for this profile, or None (never watched)."""
        row = self.conn.execute(
            "SELECT last_watch FROM watch_state WHERE profile_version=?",
            (profile_version,),
        ).fetchone()
        return datetime.fromisoformat(row["last_watch"]) if row else None

    def set_watch_marker(self, profile_version: str, when: datetime) -> None:
        self.conn.execute(
            "INSERT INTO watch_state (profile_version, last_watch) VALUES (?, ?) "
            "ON CONFLICT(profile_version) DO UPDATE SET last_watch=excluded.last_watch",
            (profile_version, _to_text(when)),
        )
        self.conn.commit()

    # ------------------------------------------------------------- trust spine
    def trust_counts(self) -> dict[str, dict[str, int]]:
        """Per-source counts of trust verdicts (LLM extractions only).

        Sources whose rows carry no ``trust_verdict`` (structured adapters) are
        absent. Read straight off the stored JSON, so it reflects exactly what
        the scrapers persisted.
        """
        rows = self.conn.execute(
            "SELECT source, json_extract(data, '$.trust_verdict') AS verdict, "
            "COUNT(*) AS n FROM opportunities "
            "WHERE json_extract(data, '$.trust_verdict') IS NOT NULL "
            "GROUP BY source, verdict"
        ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["source"], {})[r["verdict"]] = r["n"]
        return out

    def list_by_trust_verdict(
        self,
        verdict: str,
        source: str | None = None,
        now: datetime | None = None,
    ) -> list[Opportunity]:
        """Opportunities whose trust verdict matches (e.g. the quarantined set)."""
        moment = _now(now)
        where = "WHERE json_extract(data, '$.trust_verdict') = ?"
        params: list[str] = [verdict]
        if source is not None:
            where += " AND source = ?"
            params.append(source)
        # `where` is fixed clause text (no user input); values are bound via `?`.
        query = f"SELECT data FROM opportunities {where} ORDER BY id"  # nosec B608
        rows = self.conn.execute(query, params).fetchall()
        return [_load_opportunity(r["data"], moment) for r in rows]

    def backfill_trust(self, sources: set[str] | None = None) -> int:
        """Copy each cached extraction's TrustReport onto its stored opportunity.

        Coverage fix: the trust fields are EXCLUDED from ``content_hash`` (on
        purpose — re-assessing must never fake an *amended*), so the normal
        upsert sees a pre-0.12.0 row as "unchanged" and NEVER rewrites it: the
        trust spine would only cover future bandi. This rewrites the data JSON
        of every opportunity still missing a ``trust_verdict`` whose extraction
        (keyed by ``source_url``, the detail URL) has a persisted report —
        WITHOUT touching ``version`` / ``content_hash`` / ``updated_at``, so no
        fake *amended* ever reaches ``list_new`` / the watch delta. Idempotent
        (a backfilled row no longer matches the NULL filter); returns the number
        of opportunities updated.

        ``sources`` restricts the backfill to those source ids — callers pass
        the LLM-scraper set (:func:`core.run_trust_backfill`). Measured on the
        prod DB: the national ``incentivi`` hub lists the SAME regional bandi
        with ``source_url`` pointing at the regional page calabria extracted, so
        an URL-only join would stamp ``provenance="llm"`` onto rows whose fields
        actually came from the structured API. ``None`` = no restriction.
        """
        where = "WHERE json_extract(data, '$.trust_verdict') IS NULL"
        params: list[str] = []
        if sources is not None:
            if not sources:
                return 0
            placeholders = ",".join("?" for _ in sources)
            where += f" AND source IN ({placeholders})"
            params = sorted(sources)
        # `where` holds only `?` placeholders; values are bound via params.
        query = f"SELECT id, data FROM opportunities {where}"  # nosec B608
        rows = self.conn.execute(query, params).fetchall()
        updated = 0
        for r in rows:
            payload = json.loads(r["data"])
            url = payload.get("source_url")
            if not url:
                continue
            trust_row = self.conn.execute(
                "SELECT trust FROM extractions WHERE url_hash=?",
                (hashlib.sha256(url.encode("utf-8")).hexdigest(),),
            ).fetchone()
            if trust_row is None or not trust_row["trust"]:
                continue  # structured source, or extraction not assessed yet
            report = json.loads(trust_row["trust"])
            payload["provenance"] = "llm"
            payload["confidence"] = report.get("confidence")
            payload["trust_verdict"] = report.get("verdict")
            self.conn.execute(
                "UPDATE opportunities SET data=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), r["id"]),
            )
            updated += 1
        self.conn.commit()
        return updated

    # ------------------------------------------------------------- retention
    def prune(
        self,
        *,
        closed_before_days: int = 90,
        runs_before_days: int = 30,
        now: datetime | None = None,
        vacuum: bool = True,
    ) -> dict[str, int]:
        """Drop data that is no longer useful, then optionally VACUUM to reclaim space.

        Targets ONLY low-value bulk that re-accumulates:
          * ``raw_docs`` of opportunities whose deadline passed > ``closed_before_days``
            ago (the untouched source payloads — the largest rows; the normalized
            Opportunity row is kept as the dedup/change-detection ledger);
          * ``runs`` rows older than ``runs_before_days`` (per-source run audit).

        NEVER touches the things worth keeping: the score cache (``matches`` — the
        paid LLM value), ``watch_state`` markers, ``crawl_recipes`` / ``crawl_golden``,
        or the embeddings/document/extraction caches. Returns the counts removed.
        """
        moment = _now(now)
        closed_cutoff = _to_text(moment - timedelta(days=closed_before_days))
        runs_cutoff = _to_text(moment - timedelta(days=runs_before_days))

        # raw_docs linked from a long-closed opportunity (Opportunity.raw_ref -> id).
        # deadline is stored as normalized UTC ISO text, so a lexicographic `<` over
        # the cutoff ISO is a correct chronological comparison.
        raw_deleted = self.conn.execute(
            "DELETE FROM raw_docs WHERE id IN ("
            "  SELECT json_extract(data, '$.raw_ref') FROM opportunities"
            "  WHERE deadline IS NOT NULL AND deadline < ?"
            ")",
            (closed_cutoff,),
        ).rowcount
        runs_deleted = self.conn.execute(
            "DELETE FROM runs WHERE started_at IS NOT NULL AND started_at < ?",
            (runs_cutoff,),
        ).rowcount
        self.conn.commit()
        if vacuum:
            self.conn.execute("VACUUM")  # reclaim freed pages -> smaller file
        return {"raw_docs": raw_deleted, "runs": runs_deleted}


class SqliteScoreCache:
    """SQLite-backed ScoreCache (matching.relevance.ScoreCache Protocol).

    Looks matches up by the full relevance ``cache_key`` (profile, opportunity id,
    content_hash, backend + document-state fingerprint). So an amended opportunity
    (fresh content_hash), a ``--with-documents`` run, a different model, or a
    different opportunity all miss and are re-scored — never a false reuse.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _serialize(key: tuple[str, ...]) -> str:
        return "\x1f".join(key)

    def get(self, key: tuple[str, ...]) -> Match | None:
        row = self.store.conn.execute(
            "SELECT data FROM matches WHERE cache_key=?",
            (self._serialize(key),),
        ).fetchone()
        return Match.model_validate_json(row["data"]) if row else None

    def set(self, key: tuple[str, ...], match: Match) -> None:
        self.store.save_match(match, cache_key=self._serialize(key))


class SqliteDocumentCache:
    """SQLite-backed DocumentCache (documents.DocumentCache Protocol).

    Persists extracted PDF text keyed by sha256(url), so attachment PDFs are not
    re-downloaded across runs. Empty text is cached too (a failed/non-PDF fetch
    is not retried).
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def get(self, url: str) -> str | None:
        row = self.store.conn.execute(
            "SELECT text FROM documents WHERE url_hash=?", (self._key(url),)
        ).fetchone()
        return row["text"] if row else None

    def set(self, url: str, text: str) -> None:
        self.store.conn.execute(
            "INSERT INTO documents (url_hash, url, text) VALUES (?, ?, ?) "
            "ON CONFLICT(url_hash) DO UPDATE SET url=excluded.url, text=excluded.text",
            (self._key(url), url, text),
        )
        self.store.conn.commit()


class SqliteExtractionCache:
    """SQLite-backed cache of LLM-extracted bando fields, keyed by sha256(url).

    Lets the LLM-assisted scraper avoid re-paying for extraction across runs.
    The trust spine's :class:`~bandiradar.trust.TrustReport` is persisted BESIDE
    its extraction (the ``trust`` column), so a cached extraction keeps its
    deterministic verdict across runs too.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def get(self, url: str) -> dict | None:
        row = self.store.conn.execute(
            "SELECT data FROM extractions WHERE url_hash=?", (self._key(url),)
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def set(self, url: str, data: dict) -> None:
        # A re-extraction REPLACES the record, so any stored trust report no
        # longer describes it: reset it (the scraper re-assesses right after).
        self.store.conn.execute(
            "INSERT INTO extractions (url_hash, url, data, trust) "
            "VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(url_hash) DO UPDATE SET url=excluded.url, "
            "data=excluded.data, trust=NULL",
            (self._key(url), url, json.dumps(data, ensure_ascii=False)),
        )
        self.store.conn.commit()

    def get_trust(self, url: str) -> dict | None:
        row = self.store.conn.execute(
            "SELECT trust FROM extractions WHERE url_hash=?", (self._key(url),)
        ).fetchone()
        return json.loads(row["trust"]) if row and row["trust"] else None

    def set_trust(self, url: str, report: dict) -> None:
        self.store.conn.execute(
            "UPDATE extractions SET trust=? WHERE url_hash=?",
            (json.dumps(report, ensure_ascii=False), self._key(url)),
        )
        self.store.conn.commit()


class SqliteEmbeddingCache:
    """SQLite-backed EmbeddingCache (matching.embeddings.EmbeddingCache Protocol).

    Persists opportunity vectors keyed by (content_hash, model_id), so embedding is
    paid once per opportunity and reused across profiles, threshold sweeps and runs.
    An amended opportunity gets a fresh content_hash -> a clean miss -> re-embed.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self, content_hash: str, model_id: str) -> list[float] | None:
        row = self.store.conn.execute(
            "SELECT vector FROM embeddings WHERE content_hash=? AND model_id=?",
            (content_hash, model_id),
        ).fetchone()
        return json.loads(row["vector"]) if row else None

    def set(self, content_hash: str, model_id: str, vector: list[float]) -> None:
        self.store.conn.execute(
            "INSERT INTO embeddings (content_hash, model_id, vector) VALUES (?, ?, ?) "
            "ON CONFLICT(content_hash, model_id) DO UPDATE SET vector=excluded.vector",
            (content_hash, model_id, json.dumps(vector)),
        )
        self.store.conn.commit()
