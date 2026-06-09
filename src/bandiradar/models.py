"""Canonical data model — THE contract (see ARCHITECTURE.md §4).

Defines the pydantic v2 models shared across the whole engine:

- :class:`Opportunity` — the superset model covering tenders AND
  grants/incentives.
- :class:`RawDoc` — the untouched payload from a source (audit + re-mapping).
- :class:`Profile` — the company we match opportunities against (§7).
- :class:`Match` — the Stage-2 relevance result for an (opportunity, profile)
  pair.

Do NOT break field names/shape without updating ARCHITECTURE.md and every
adapter + test in the same change.

Design notes (see the Prompt 1 spec):
- ``content_hash`` is derived from ONLY the semantically meaningful fields and
  deliberately EXCLUDES ``version`` and ``updated_at`` — otherwise change
  detection (ARCHITECTURE.md §8) would fire on every re-fetch.
- ``status`` is PURELY lifecycle (open/closing_soon/closed) and is DERIVED from
  ``deadline`` + the current time at READ time (``default_status``); storage
  recomputes it on the way out, so a stored item never shows "open" past its
  deadline. The "this notice changed" signal is tracked separately via ``version``
  + ``updated_at`` (storage change-detection / ``list_new``), not in ``status``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "CLOSING_SOON_DAYS",
    "Kind",
    "GeoScope",
    "Status",
    "FetchStatus",
    "FetchErrorKind",
    "Opportunity",
    "RawDoc",
    "ValueRange",
    "Profile",
    "Match",
    "SourceResult",
    "DoctorSourceResult",
    "DoctorEnv",
    "DoctorReport",
    "default_status",
    "sanitize_value_bounds",
]

# Days-before-deadline window in which an opportunity counts as "closing soon".
CLOSING_SOON_DAYS = 7

Kind = Literal["tender", "grant", "incentive"]
# What a company pursues: GRANTS/incentives (applied for) vs public TENDERS (bid on,
# i.e. selling to the PA). A profile may seek either or both; see Profile.seeks and
# the Stage-1 instrument gate. Opportunity ``kind`` maps to a seek class: tenders ->
# "tender", grants/incentives -> "grant" (see ``matching.prefilter.seek_class``).
Seek = Literal["grant", "tender"]
GeoScope = Literal["national", "regional", "eu", "local"]
# ``status`` is PURELY the lifecycle of the deadline (open / closing soon / closed).
# It is DERIVED from ``deadline`` + the current time at READ time (see
# ``default_status``), so a stored item never shows "open" past its deadline. The
# "this notice changed" signal does NOT live here — it is tracked separately via
# ``version`` + ``updated_at`` (storage change-detection), surfaced by the watch
# delta / ``list_new``. (Pre-0.2.0 used a sticky ``"amended"`` status; removed.)
Status = Literal["open", "closing_soon", "closed"]
# Outcome of fetching ONE source in a run (observability — see SourceResult):
#   ok       -> records fetched, fetch completed cleanly
#   partial  -> fetch raised mid-stream, but records already saved are kept
#   failed   -> fetch raised before saving anything (nothing ingested)
#   empty    -> fetch completed cleanly but yielded no records
FetchStatus = Literal["ok", "partial", "failed", "empty"]
# Structured cause of a failed/partial fetch (no string-matching downstream):
#   rate_limited -> HTTP 429 / throttled
#   unavailable  -> 5xx / timeout / connection error (source down or unreachable)
#   invalid      -> the payload could not be parsed/validated
#   unknown      -> anything else (non-FetchError exceptions)
FetchErrorKind = Literal["rate_limited", "unavailable", "invalid", "unknown"]

# The fields that feed content_hash — i.e. the ones whose change makes an
# opportunity semantically different and so re-notifiable (ARCHITECTURE.md §8).
# version, updated_at, keywords, and ateco_hints are intentionally absent:
# they are bookkeeping / derived hints, not the substance of the notice.
# document_urls / document_text are also absent ON PURPOSE: they are optional
# downstream enrichment (fetched attachment text), not source-of-truth content —
# including them would trip change-detection (a spurious amend) just because we
# ran enrichment.
_CONTENT_HASH_FIELDS = (
    "title",
    "summary",
    "issuer_name",
    "issuer_region",
    "value_amount",
    "value_currency",
    "value_min",
    "value_max",
    "deadline",
    "eligibility_text",
    "kind",
    "cpv",
    "region",
    "geo_scope",
)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to tz-aware UTC.

    Naive inputs are assumed to be UTC; aware inputs are converted to UTC. This
    keeps the whole engine on a single, comparable representation: naive-vs-aware
    comparisons never raise, AND the same instant always serializes identically
    (so content_hash does not flip just because a source used +02:00 vs Z).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def sanitize_value_bounds(
    value_min: float | None, value_max: float | None
) -> tuple[float | None, float | None]:
    """Repair an obviously transposed min/max pair before model construction.

    :class:`Opportunity` rejects ``value_min > value_max`` (fail-loud contract).
    Real sources occasionally emit the two swapped, which is dirty data, not a
    bug in our model — so mappers call this to swap a transposed pair instead of
    letting one bad record abort the whole ingestion. Anything else (a single
    bound, ``None``s, already-ordered values) passes through untouched.
    """
    if value_min is not None and value_max is not None and value_min > value_max:
        return value_max, value_min
    return value_min, value_max


def default_status(
    deadline: datetime | None, now: datetime | None = None
) -> Literal["open", "closing_soon", "closed"]:
    """Derive the lifecycle status from a deadline (the single source of truth).

    - no deadline -> ``"open"``
    - deadline in the past -> ``"closed"``
    - deadline within :data:`CLOSING_SOON_DAYS` -> ``"closing_soon"``
    - otherwise -> ``"open"``

    ``now`` defaults to the current UTC time; tests pass an explicit value for
    determinism. Naive ``now``/``deadline`` are defensively coerced to UTC, so
    callers cannot trigger a naive-vs-aware crash here. Storage calls this on every
    read so ``status`` is always current; the "changed" signal is separate
    (``version`` / ``updated_at``).
    """
    now = datetime.now(UTC) if now is None else _ensure_utc(now)
    deadline = _ensure_utc(deadline)
    if deadline is None:
        return "open"
    if deadline < now:
        return "closed"
    if deadline <= now + timedelta(days=CLOSING_SOON_DAYS):
        return "closing_soon"
    return "open"


class Opportunity(BaseModel):
    """A single funding opportunity — the canonical superset (ARCHITECTURE.md §4)."""

    model_config = ConfigDict(extra="forbid")  # a mis-mapped adapter field fails loudly

    id: str  # stable, source-prefixed: "anac:<ocid>"
    source: str  # source id, e.g. "anac"
    source_url: str
    kind: Kind

    title: str
    summary: str | None = None
    issuer_name: str | None = None  # buyer / granting body
    issuer_region: str | None = None

    cpv: list[str] = Field(default_factory=list)  # tender procurement codes
    ateco_hints: list[str] = Field(default_factory=list)  # mapped/declared codes
    keywords: list[str] = Field(default_factory=list)

    value_amount: float | None = None
    value_currency: str = "EUR"
    value_min: float | None = None
    value_max: float | None = None

    geo_scope: GeoScope
    region: str | None = None

    published_at: datetime | None = None
    deadline: datetime | None = None
    updated_at: datetime | None = None
    status: Status  # stored, freely settable (see module docstring)

    eligibility_text: str | None = None  # free text fed to the matcher
    document_urls: list[str] = Field(default_factory=list)  # attachment/doc links
    document_text: str | None = None  # text extracted from those docs (enrichment)
    raw_ref: str  # pointer to stored RawDoc
    content_hash: str = ""  # for change detection; auto-filled if left empty
    version: int = 1

    @field_validator("published_at", "deadline", "updated_at")
    @classmethod
    def _coerce_datetimes_to_utc(cls, v: datetime | None) -> datetime | None:
        return _ensure_utc(v)

    @model_validator(mode="after")
    def _validate_and_fill(self) -> Opportunity:
        if (
            self.value_min is not None
            and self.value_max is not None
            and self.value_min > self.value_max
        ):
            raise ValueError("value_min must not exceed value_max")
        # Auto-populate content_hash when a caller did not supply one. Assignment
        # does not re-trigger validation (validate_assignment is off), so this is
        # safe and loop-free.
        if not self.content_hash:
            self.content_hash = self.compute_content_hash()
        return self

    def compute_content_hash(self) -> str:
        """Deterministic SHA-256 over ONLY the semantically meaningful fields.

        Excludes ``version`` and ``updated_at`` by construction (see
        :data:`_CONTENT_HASH_FIELDS`). Same meaningful content -> same hash
        across runs and processes.
        """
        payload: dict[str, Any] = {}
        for field in _CONTENT_HASH_FIELDS:
            value = getattr(self, field)
            payload[field] = value.isoformat() if isinstance(value, datetime) else value
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RawDoc(BaseModel):
    """An untouched source payload, kept for audit and re-mapping (§4/§5)."""

    id: str  # stable, source-prefixed (mirrors the Opportunity raw_ref)
    source: str
    fetched_at: datetime
    payload: dict[str, Any]  # the original record, verbatim
    url: str | None = None

    @field_validator("fetched_at")
    @classmethod
    def _coerce_fetched_at_to_utc(cls, v: datetime) -> datetime:
        coerced = _ensure_utc(v)
        assert coerced is not None  # fetched_at is required, never None
        return coerced


class ValueRange(BaseModel):
    """Inclusive monetary range a profile is interested in."""

    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _check_order(self) -> ValueRange:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("value_range.min must not exceed value_range.max")
        return self


class Profile(BaseModel):
    """The company we match opportunities against (ARCHITECTURE.md §7)."""

    model_config = ConfigDict(extra="forbid")  # a stray profile key fails loudly

    name: str
    language: str = "it"
    ateco: list[str] = Field(default_factory=list)
    cpv_interests: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)  # free-text match terms
    regions: list[str] = Field(default_factory=list)
    value_range: ValueRange = Field(default_factory=ValueRange)
    capabilities: str = ""  # free text fed to the matcher
    exclusions: list[str] = Field(default_factory=list)
    # Which instrument classes this company pursues. Default BOTH — so unset
    # profiles match grants AND tenders exactly as before (no false drops). A
    # grant-only profile (e.g. an AI studio) drops public tenders at Stage 1; a
    # firm that sells to the PA (construction, medical supply) keeps ["grant",
    # "tender"]. See the Stage-1 instrument gate.
    seeks: list[Seek] = Field(default_factory=lambda: ["grant", "tender"])

    @property
    def version(self) -> str:
        """Stable content hash of the profile, used as a match-cache key (§6).

        Deterministic across runs; changes iff any profile field changes.
        """
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Match(BaseModel):
    """Stage-2 relevance result for one (opportunity, profile) pair (§6).

    The relevance cache key is ``(profile_version, opportunity_hash)``. Carrying
    ``opportunity_hash`` (the Opportunity.content_hash at scoring time) lets the
    cache self-invalidate: when an opportunity is amended its content_hash
    changes, so the old Match no longer matches the new key and is re-scored.
    """

    opportunity_id: str
    opportunity_hash: str  # Opportunity.content_hash at scoring time -> cache key part
    profile_version: str  # Profile.version at scoring time -> cache key part
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    matched_capabilities: list[str] = Field(default_factory=list)
    eligibility_flags: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class SourceResult(BaseModel):
    """Structured outcome of fetching ONE source in a run — the single source of
    truth for "what happened" (returned, persisted, printed, and logged).

    This is observability, not the canonical contract: it captures counts, the
    derived :data:`FetchStatus`, a clean (secret-free) error string, and timing,
    so per-source isolation and debugging start ahead.
    """

    source: str
    status: FetchStatus
    fetched: int = 0  # raw records pulled from the source
    mapped: int = 0  # opportunities produced by to_opportunities
    skipped_invalid: int = 0  # records quarantined (failed to map/validate)
    new: int = 0  # store upserts that were new
    amended: int = 0  # store upserts that changed an existing opportunity
    error: str | None = None  # clean message when status is partial/failed
    error_kind: FetchErrorKind | None = None  # structured cause (None when no error)
    duration_s: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Crawl/listing health for scraper sources (ok/degraded/broken); None otherwise.
    # Makes a crawl gone into drift visible in the result, not silent.
    crawl_health: str | None = None

    @property
    def ok(self) -> bool:
        """True when the source did not error (``ok`` or ``empty``)."""
        return self.status in ("ok", "empty")


class DoctorSourceResult(BaseModel):
    """Health of ONE source from ``bandiradar doctor`` (a lightweight live probe)."""

    source: str
    needs_key: bool  # the source requires an LLM provider/key to run
    key_ok: bool | None  # is that key configured? (None when not key-dependent)
    reachable: bool | None  # None when not probed (needs key, none configured)
    parsed: bool  # the first probed record mapped/validated cleanly
    status: str  # FetchStatus, or "needs_key" when the probe was skipped
    error_kind: FetchErrorKind | None = None
    note: str | None = None
    # Crawl/listing health for scraper sources (ok/degraded/broken). The crawl is
    # key-less, so this is reported even when the source is "needs_key". None for
    # sources without a crawl recipe.
    crawl_health: str | None = None


class DoctorEnv(BaseModel):
    """Environment-level diagnostics (no LLM calls made)."""

    python_version: str
    llm_provider: str  # "none" | "anthropic" | "openai"
    llm_key_present: bool
    llm_ready: bool  # provider set + key present + SDK importable
    extras: dict[str, bool]  # optional extras installed (anthropic/openai/ocr)
    db_ok: bool
    db_error: str | None = None


class DoctorReport(BaseModel):
    """Assembled health report: per-source probes + environment + verdict."""

    sources: list[DoctorSourceResult]
    env: DoctorEnv
    healthy: bool
    exit_code: int
