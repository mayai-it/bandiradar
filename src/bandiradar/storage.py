"""SQLite store: persistence + dedupe + change-detection (ARCHITECTURE.md §8).

Stdlib ``sqlite3``, no ORM. Holds opportunities, raw docs, matches, and run
bookkeeping, and backs the Stage-2 relevance cache (:class:`SqliteScoreCache`).
Change detection is driven by ``content_hash`` (§8): a changed hash bumps the
version, sets status ``"amended"``, and makes the row a re-notifiable
*rettifica*. This module is persistence only — orchestration lives in core.

The ``"amended"`` status stays until the opportunity is re-derived; clearing or
acknowledging it is a later delivery concern.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from bandiradar.models import Match, Opportunity, RawDoc

UpsertResult = Literal["new", "unchanged", "amended"]

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
    score            INTEGER NOT NULL,
    data             TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, profile_version)
);

CREATE INDEX IF NOT EXISTS idx_matches_cache
    ON matches (profile_version, opportunity_hash);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT,
    started_at  TEXT,
    finished_at TEXT,
    fetched     INTEGER,
    "new"       INTEGER,
    amended     INTEGER
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
    data     TEXT NOT NULL       -- JSON of the LLM-extracted bando fields
);
"""


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
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

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

        # Amended: bump version, mark amended, stamp updated_at — persisted into
        # the stored Opportunity JSON too so reads reflect the rettifica.
        amended = opp.model_copy(
            update={
                "version": row["version"] + 1,
                "status": "amended",
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

    def get_opportunity(self, opp_id: str) -> Opportunity | None:
        row = self.conn.execute(
            "SELECT data FROM opportunities WHERE id=?", (opp_id,)
        ).fetchone()
        return Opportunity.model_validate_json(row["data"]) if row else None

    def list_opportunities(
        self, status: str | None = None, source: str | None = None
    ) -> list[Opportunity]:
        clauses: list[str] = []
        params: list[str] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if source is not None:
            clauses.append("source=?")
            params.append(source)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT data FROM opportunities{where} ORDER BY id", params
        ).fetchall()
        return [Opportunity.model_validate_json(r["data"]) for r in rows]

    def list_new(self, since: datetime | None) -> list[Opportunity]:
        """Opportunities changed after ``since`` (all when ``since`` is None).

        "Changed" = last store mutation: updated_at if amended, else inserted_at.
        This is the monitor's new/amended feed.
        """
        rows = self.conn.execute(
            "SELECT data, inserted_at, updated_at FROM opportunities"
        ).fetchall()
        threshold = _now(since) if since is not None else None
        out: list[tuple[datetime, Opportunity]] = []
        for row in rows:
            changed_text = row["updated_at"] or row["inserted_at"]
            changed_at = datetime.fromisoformat(changed_text)
            if threshold is None or changed_at > threshold:
                out.append((changed_at, Opportunity.model_validate_json(row["data"])))
        out.sort(key=lambda pair: pair[0])
        return [opp for _, opp in out]

    # ----------------------------------------------------------------- matches
    def save_match(self, match: Match) -> None:
        """Upsert a match by (opportunity_id, profile_version)."""
        self.conn.execute(
            "INSERT INTO matches "
            "(opportunity_id, profile_version, opportunity_hash, score, "
            " data, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(opportunity_id, profile_version) DO UPDATE SET "
            "opportunity_hash=excluded.opportunity_hash, score=excluded.score, "
            "data=excluded.data, created_at=excluded.created_at",
            (
                match.opportunity_id,
                match.profile_version,
                match.opportunity_hash,
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
            'INSERT INTO runs (source, started_at, fetched, "new", amended) '
            "VALUES (?, ?, 0, 0, 0)",
            (source, _to_text(_now(started_at))),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        fetched: int,
        new: int,
        amended: int,
        finished_at: datetime | None = None,
    ) -> None:
        self.conn.execute(
            'UPDATE runs SET finished_at=?, fetched=?, "new"=?, amended=? WHERE id=?',
            (_to_text(_now(finished_at)), fetched, new, amended, run_id),
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


class SqliteScoreCache:
    """SQLite-backed ScoreCache (matching.relevance.ScoreCache Protocol).

    Looks matches up by (profile_version, opportunity_hash). An amended
    opportunity has a fresh content_hash, so it naturally misses and is re-scored.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self, key: tuple[str, str]) -> Match | None:
        profile_version, opportunity_hash = key
        row = self.store.conn.execute(
            "SELECT data FROM matches WHERE profile_version=? AND opportunity_hash=?",
            (profile_version, opportunity_hash),
        ).fetchone()
        return Match.model_validate_json(row["data"]) if row else None

    def set(self, key: tuple[str, str], match: Match) -> None:
        self.store.save_match(match)


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
        self.store.conn.execute(
            "INSERT INTO extractions (url_hash, url, data) VALUES (?, ?, ?) "
            "ON CONFLICT(url_hash) DO UPDATE SET url=excluded.url, data=excluded.data",
            (self._key(url), url, json.dumps(data, ensure_ascii=False)),
        )
        self.store.conn.commit()
