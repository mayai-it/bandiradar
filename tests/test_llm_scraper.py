"""Self-healing spine tests — crawl recipes, drift detection, golden validator.

Offline, no LLM, no network: the golden listing is the recorded cassette
``tests/cassettes/toscana_listing.json``."""

import json
from pathlib import Path

from bandiradar import core
from bandiradar.sources import toscana
from bandiradar.sources.llm_scraper import (
    CrawlRecipe,
    apply_recipe,
    recipe_reproduces_golden,
    validate_refs,
)

GOLDEN = Path(__file__).parent / "cassettes" / "toscana_listing.json"


def golden_listing() -> list:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# apply_recipe — the data recipe reproduces the known-good refs
# --------------------------------------------------------------------------- #


def test_apply_recipe_reproduces_known_refs_from_golden():
    listing = golden_listing()
    refs = apply_recipe(toscana.TOSCANA_RECIPE, listing)
    assert len(refs) == len(listing) and refs  # one ref per listing item
    # each ref matches the item's id / link / title.rendered
    for ref, item in zip(refs, listing, strict=True):
        assert ref == (item["id"], item["link"], item["title"]["rendered"])
    assert validate_refs(refs) == "ok"


def test_apply_recipe_tolerates_a_non_list():
    assert apply_recipe(toscana.TOSCANA_RECIPE, {"not": "a list"}) == []


# --------------------------------------------------------------------------- #
# validate_refs — drift detector
# --------------------------------------------------------------------------- #


def test_validate_refs_health_levels():
    assert validate_refs([]) == "broken"  # zero refs
    assert validate_refs([(1, "", ""), (2, "", "")]) == "broken"  # all empty
    assert validate_refs([(1, "u", "")]) == "broken"  # title empty -> unusable
    assert validate_refs([(1, "u", "t"), (2, "", "")]) == "degraded"  # some empty
    assert validate_refs([(1, "u", "t"), (2, "v", "s")]) == "ok"


def test_wrong_recipe_drifts_to_broken_on_golden():
    # A recipe with wrong field paths yields empty links/titles -> broken.
    wrong = CrawlRecipe(listing_url="x", detail_url_path="nope", title_path="also.nope")
    assert validate_refs(apply_recipe(wrong, golden_listing())) == "broken"


# --------------------------------------------------------------------------- #
# recipe_reproduces_golden — the gate a (future agent-derived) recipe must pass
# --------------------------------------------------------------------------- #


def test_golden_validator_accepts_default_rejects_wrong():
    listing = golden_listing()
    expected = apply_recipe(toscana.TOSCANA_RECIPE, listing)  # known-good refs
    assert recipe_reproduces_golden(toscana.TOSCANA_RECIPE, listing, expected) is True
    # a wrong recipe (detail_url from the wrong field) does NOT reproduce them
    wrong = CrawlRecipe(
        listing_url=toscana.TOSCANA_LIST_URL,
        post_id_path="id",
        detail_url_path="slug",  # WP item has no "slug" in this listing
        title_path="title.rendered",
    )
    assert recipe_reproduces_golden(wrong, listing, expected) is False


# --------------------------------------------------------------------------- #
# doctor surfaces crawl health key-lessly (drift visible without an LLM key)
# --------------------------------------------------------------------------- #


def test_doctor_surfaces_crawl_health_without_key(monkeypatch, tmp_path):
    class _Scraper:
        id = "toscana"
        kind = "incentive"
        requires_llm = True

        def crawl_health(self):
            return "degraded"

        def load_fixture(self):
            return []

    source = _Scraper()
    monkeypatch.setattr(core, "list_sources", lambda: [source])
    monkeypatch.setattr(core, "get", lambda _sid: source)
    # conftest forces BANDIRADAR_LLM_PROVIDER=none -> needs_key branch (not probed)
    report = core.run_doctor(db=str(tmp_path / "d.db"))
    r = report.sources[0]
    assert r.status == "needs_key"
    assert r.crawl_health == "degraded"
    assert "crawl: degraded" in (r.note or "")


# --------------------------------------------------------------------------- #
# Trust spine — the deterministic gate over each LLM extraction (offline)
# --------------------------------------------------------------------------- #

from datetime import UTC, datetime, timedelta  # noqa: E402

from bandiradar.sources.llm_scraper import (  # noqa: E402
    InMemoryExtractionCache,
    LlmScraperSource,
)

_DEADLINE = (datetime.now(UTC) + timedelta(days=30)).date()
_PAGE = (
    "Bando per la transizione digitale delle PMI. Dotazione € 1.000.000. "
    f"Scadenza {_DEADLINE.day:02d}/{_DEADLINE.month:02d}/{_DEADLINE.year}."
)
_EXTRACTION = {
    "title": "Bando per la transizione digitale delle PMI",
    "summary": "Contributi alle PMI",
    "eligibility_text": "PMI",
    "value_amount": 1_000_000.0,
    "value_min": None,
    "value_max": None,
    "deadline": _DEADLINE.isoformat(),
    "keywords": ["digitale"],
    "kind": "incentive",
}


class _FakeLLM:
    def __init__(self, reply: dict):
        self.reply = reply
        self.calls = 0

    def score(self, system: str, user: str) -> dict:
        self.calls += 1
        return dict(self.reply)


class _FakeRegion(LlmScraperSource):
    id = "fakeregione"
    region = "FakeRegione"
    issuer_name = "Ente Fake"
    listing_url = "https://fake.invalid/bandi"

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.text_fetches = 0

    def _listing_refs(self):
        return [(i + 1, url, f"Bando {i + 1}") for i, url in enumerate(self.pages)]

    def _fetch_text(self, url: str) -> str:
        self.text_fetches += 1
        return self.pages[url]


def test_scrape_assesses_and_persists_trust():
    src = _FakeRegion({"https://fake.invalid/bandi/1": _PAGE})
    cache = InMemoryExtractionCache()
    raws = list(src.fetch(client=_FakeLLM(_EXTRACTION), cache=cache))

    [raw] = raws
    trust = raw.payload["_trust"]
    assert trust["verdict"] == "ok"
    assert trust["checks"]["deadline_in_text"] is True
    # the report persists BESIDE the cached extraction
    assert cache.get_trust("https://fake.invalid/bandi/1") == trust

    [opp] = src.to_opportunities(raw)
    assert opp.provenance == "llm"
    assert opp.confidence == trust["confidence"]
    assert opp.trust_verdict == "ok"


def test_scrape_quarantines_hallucinated_deadline():
    page = "Bando per la transizione digitale delle PMI. Nessuna data qui."
    src = _FakeRegion({"https://fake.invalid/bandi/1": page})
    raws = list(
        src.fetch(
            client=_FakeLLM(_EXTRACTION),  # asserts a deadline the page lacks
            cache=InMemoryExtractionCache(),
        )
    )
    [opp] = src.to_opportunities(raws[0])
    assert opp.trust_verdict == "quarantine"
    assert opp.provenance == "llm"


def test_cache_hit_with_trust_skips_fetch_and_llm():
    src = _FakeRegion({"https://fake.invalid/bandi/1": _PAGE})
    cache = InMemoryExtractionCache()
    client = _FakeLLM(_EXTRACTION)
    list(src.fetch(client=client, cache=cache))
    assert client.calls == 1 and src.text_fetches == 1

    # 2nd run: extraction AND report cached -> no page fetch, no LLM call.
    raws = list(src.fetch(client=client, cache=cache))
    assert client.calls == 1
    assert src.text_fetches == 1
    assert raws[0].payload["_trust"]["verdict"] == "ok"


def test_legacy_cache_hit_backfills_trust_without_llm():
    # A pre-0.12.0 cache row has the extraction but NO report: backfill it with
    # ONE page fetch (HTTP only — the LLM is never re-paid).
    src = _FakeRegion({"https://fake.invalid/bandi/1": _PAGE})
    cache = InMemoryExtractionCache()
    cache.set("https://fake.invalid/bandi/1", dict(_EXTRACTION))
    client = _FakeLLM(_EXTRACTION)

    raws = list(src.fetch(client=client, cache=cache))
    assert client.calls == 0
    assert src.text_fetches == 1
    assert raws[0].payload["_trust"]["verdict"] == "ok"
    assert cache.get_trust("https://fake.invalid/bandi/1") is not None


def test_mapper_tolerates_missing_trust():
    # Old fixtures / cache rows carry no _trust: provenance is still honest,
    # confidence/verdict simply unknown.
    from bandiradar.models import RawDoc

    src = _FakeRegion({})
    raw = RawDoc(
        id="fakeregione:1",
        source="fakeregione",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload={**_EXTRACTION, "_post_id": 1, "_url": "u", "_listing_title": "B"},
    )
    [opp] = src.to_opportunities(raw)
    assert opp.provenance == "llm"
    assert opp.confidence is None
    assert opp.trust_verdict is None
