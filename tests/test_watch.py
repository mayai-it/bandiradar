"""watch monitor loop + exporters tests (Prompt 14). Offline, tmp db, fixed now."""

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

import pytest

from bandiradar import core, exporters, resources
from bandiradar.sources import incentivi
from bandiradar.storage import Store

NOW = datetime(2026, 6, 4, 0, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)


def mayai():
    return core.load_profile("mayai")


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "watch.db"))
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# run_watch delta semantics
# --------------------------------------------------------------------------- #


def test_first_run_all_new_second_run_none(store):
    results, first = core.run_watch(
        mayai(), store, source_ids=["incentivi"], sample=True, now=NOW
    )
    assert {o.id for o, _ in first} == {"incentivi:3400"}  # the one mayai match
    assert [r.source for r in results] == ["incentivi"]
    assert results[0].status == "ok"

    _, second = core.run_watch(
        mayai(), store, source_ids=["incentivi"], sample=True, now=NOW
    )
    assert second == []  # nothing changed since the marker


def test_skip_fetch_reuses_db_and_keeps_per_profile_delta(store):
    # Profile mayai fetches the source ONCE (the only live fetch of the run).
    res_fetch, delta_fetch = core.run_watch(
        mayai(), store, source_ids=["incentivi"], sample=True, now=NOW
    )
    assert [r.source for r in res_fetch] == ["incentivi"]  # fetched
    assert {o.id for o, _ in delta_fetch} == {"incentivi:3400"}

    # A SECOND profile reuses the already-fetched data with NO fetch of its own.
    other = core.load_profile("manifattura")
    res_skip, delta_skip = core.run_watch(
        other, store, source_ids=["incentivi"], sample=True, fetch=False, now=NOW
    )
    assert res_skip == []  # the skip-fetch path fetches nothing

    # Per-profile correctness: the skip delta is EXACTLY what `other` gets if it
    # fetches the same source itself in a fresh DB (skip-fetch changes cost, not
    # the result).
    control = Store(":memory:")
    try:
        _, delta_control = core.run_watch(
            other, control, source_ids=["incentivi"], sample=True, now=NOW
        )
    finally:
        control.close()
    assert {o.id for o, _ in delta_skip} == {o.id for o, _ in delta_control}


def test_skip_fetch_advances_marker_so_second_run_is_empty(store):
    core.run_watch(mayai(), store, source_ids=["incentivi"], sample=True, now=NOW)
    other = core.load_profile("manifattura")
    # First skip-fetch run: marker was None -> sees the shared data, advances marker.
    core.run_watch(
        other, store, source_ids=["incentivi"], sample=True, fetch=False, now=NOW
    )
    # Second skip-fetch run later: marker advanced, nothing changed (no fetch) -> [].
    _, second = core.run_watch(
        other, store, source_ids=["incentivi"], sample=True, fetch=False, now=LATER
    )
    assert second == []


def test_amended_record_reappears(tmp_path, monkeypatch, store):
    # Use a temp copy of the fixture so we can mutate it between runs.
    fixture = tmp_path / "incentivi.json"
    fixture.write_bytes(resources.fixture("incentivi.json").read_bytes())
    monkeypatch.setattr(incentivi, "FIXTURE_PATH", fixture)

    core.run_watch(mayai(), store, source_ids=["incentivi"], sample=True, now=NOW)
    _, again = core.run_watch(
        mayai(), store, source_ids=["incentivi"], sample=True, now=NOW
    )
    assert again == []

    # Mutate the matched record's title (changes content_hash -> amended).
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for doc in data["docs"]:
        if "3400" in doc.get("ss_search_api_id", ""):
            doc["tum_X3b_it_title_ft"] = [
                "RETTIFICA - servizi di digitalizzazione e intelligenza artificiale"
            ]
    fixture.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    _, delta = core.run_watch(
        mayai(), store, source_ids=["incentivi"], sample=True, now=LATER
    )
    by_id = {o.id: o for o, _ in delta}
    assert "incentivi:3400" in by_id  # the amended notice resurfaces in the delta
    # Change-state lives in version now (>1 == amended), NOT in lifecycle status.
    assert by_id["incentivi:3400"].version == 2
    assert by_id["incentivi:3400"].status in ("open", "closing_soon", "closed")


# --------------------------------------------------------------------------- #
# exporters
# --------------------------------------------------------------------------- #


def _sample_matches(store):
    return core.run_watch(
        mayai(), store, source_ids=["incentivi"], sample=True, now=NOW
    )[1]


def test_to_json_matches_shape(store):
    matches = _sample_matches(store)
    data = json.loads(exporters.to_json(matches))
    assert isinstance(data, list) and data
    assert set(data[0].keys()) == {
        "opportunity_id",
        "score",
        "status",
        "title",
        "deadline",
        "reasons",
        "matched_capabilities",
        "source_url",
    }


def test_to_rss_is_valid_xml(store):
    matches = _sample_matches(store)
    xml = exporters.to_rss(matches)
    root = ET.fromstring(xml)  # raises on invalid XML
    assert root.tag == "rss" and root.get("version") == "2.0"
    items = root.findall("./channel/item")
    assert len(items) == len(matches)
    item = items[0]
    assert item.find("title") is not None
    assert item.find("guid").get("isPermaLink") == "false"
    assert item.find("guid").text.startswith("incentivi:")


def test_to_rss_empty_is_still_valid():
    root = ET.fromstring(exporters.to_rss([]))
    assert root.tag == "rss"
    assert root.findall("./channel/item") == []
