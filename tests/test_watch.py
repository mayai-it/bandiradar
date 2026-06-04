"""watch monitor loop + exporters tests (Prompt 14). Offline, tmp db, fixed now."""

import json
import shutil
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from bandiradar import core, exporters
from bandiradar.sources import incentivi
from bandiradar.storage import Store

NOW = datetime(2026, 6, 4, 0, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
PROFILES = Path(__file__).resolve().parents[1] / "data" / "profiles"
INCENTIVI_FIXTURE = (
    Path(__file__).resolve().parents[1] / "data" / "fixtures" / "incentivi.json"
)


def mayai():
    data = yaml.safe_load((PROFILES / "mayai.yaml").read_text(encoding="utf-8"))
    from bandiradar.models import Profile

    return Profile(**data)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "watch.db"))
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# run_watch delta semantics
# --------------------------------------------------------------------------- #


def test_first_run_all_new_second_run_none(store):
    first = core.run_watch(
        mayai(), store, source_ids=["incentivi"], sample=True, now=NOW
    )
    assert {o.id for o, _ in first} == {"incentivi:3400"}  # the one mayai match

    second = core.run_watch(
        mayai(), store, source_ids=["incentivi"], sample=True, now=NOW
    )
    assert second == []  # nothing changed since the marker


def test_amended_record_reappears(tmp_path, monkeypatch, store):
    # Use a temp copy of the fixture so we can mutate it between runs.
    fixture = tmp_path / "incentivi.json"
    shutil.copy(INCENTIVI_FIXTURE, fixture)
    monkeypatch.setattr(incentivi, "FIXTURE_PATH", fixture)

    core.run_watch(mayai(), store, source_ids=["incentivi"], sample=True, now=NOW)
    assert core.run_watch(
        mayai(), store, source_ids=["incentivi"], sample=True, now=NOW
    ) == []

    # Mutate the matched record's title (changes content_hash -> amended).
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for doc in data["docs"]:
        if "3400" in doc.get("ss_search_api_id", ""):
            doc["tum_X3b_it_title_ft"] = [
                "RETTIFICA - servizi di digitalizzazione e intelligenza artificiale"
            ]
    fixture.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    delta = core.run_watch(
        mayai(), store, source_ids=["incentivi"], sample=True, now=LATER
    )
    by_id = {o.id: o for o, _ in delta}
    assert "incentivi:3400" in by_id
    assert by_id["incentivi:3400"].status == "amended"


# --------------------------------------------------------------------------- #
# exporters
# --------------------------------------------------------------------------- #


def _sample_matches(store):
    return core.run_watch(
        mayai(), store, source_ids=["incentivi"], sample=True, now=NOW
    )


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
