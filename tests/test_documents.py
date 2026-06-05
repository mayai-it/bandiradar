"""Document-enrichment tests — OFFLINE, no network, no tesseract required."""

from datetime import UTC, datetime
from pathlib import Path

from bandiradar import documents
from bandiradar.documents import InMemoryDocumentCache, enrich, extract_pdf_text
from bandiradar.matching.prefilter import prefilter
from bandiradar.models import Opportunity, Profile
from bandiradar.storage import SqliteDocumentCache, Store

NOW = datetime(2026, 6, 4, 0, 0, tzinfo=UTC)
SAMPLE_PDF = (
    Path(__file__).resolve().parents[1] / "data" / "fixtures" / "sample_bando.pdf"
)
PDF_BYTES = SAMPLE_PDF.read_bytes()


def opp(**overrides) -> Opportunity:
    base = dict(
        id="x:1",
        source="x",
        source_url="https://example.invalid/x",
        kind="tender",
        title="Bando generico per un servizio",
        summary=None,
        geo_scope="national",
        status="open",
        cpv=[],
        raw_ref="x:1",
    )
    base.update(overrides)
    return Opportunity(**base)


# --------------------------------------------------------------------------- #
# extract_pdf_text
# --------------------------------------------------------------------------- #


def test_extract_pdf_text_from_fixture():
    text = extract_pdf_text(PDF_BYTES).lower()
    assert "domotica" in text
    assert "efficientamento" in text
    assert "durc" in text


def test_extract_garbage_returns_empty_no_raise():
    assert extract_pdf_text(b"this is not a pdf at all") == ""


def test_ocr_skipped_cleanly_when_unavailable():
    # pytesseract/pdf2image are an optional extra, not installed by default ->
    # the OCR fallback must return "" without raising.
    assert documents._ocr_pdf(b"%PDF-1.4 not really") == ""


# --------------------------------------------------------------------------- #
# fetch_and_extract (mocked client — no network)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, content: bytes, content_type="application/pdf", boom=False):
        self.content = content
        self.headers = {"content-type": content_type}
        self._boom = boom

    def raise_for_status(self):
        if self._boom:
            raise RuntimeError("http 500")


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def get(self, url):
        return self._response


def test_fetch_and_extract_reads_pdf():
    client = _FakeClient(_FakeResponse(PDF_BYTES))
    text = documents.fetch_and_extract("https://x/doc.pdf", client=client).lower()
    assert "domotica" in text


def test_fetch_and_extract_non_pdf_returns_empty():
    client = _FakeClient(_FakeResponse(b"<html>nope</html>", content_type="text/html"))
    assert documents.fetch_and_extract("https://x/page", client=client) == ""


def test_fetch_and_extract_http_error_returns_empty():
    client = _FakeClient(_FakeResponse(b"", boom=True))
    assert documents.fetch_and_extract("https://x/doc.pdf", client=client) == ""


# --------------------------------------------------------------------------- #
# enrich (mocked fetcher) + caching
# --------------------------------------------------------------------------- #


def test_enrich_fills_document_text_and_caches(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(url, client=None):
        calls["n"] += 1
        return "testo del disciplinare con domotica"

    monkeypatch.setattr(documents, "fetch_and_extract", fake_fetch)
    cache = InMemoryDocumentCache()
    o = opp(document_urls=["https://x/a.pdf"])

    first = enrich(o, cache=cache)
    assert first.document_text and "domotica" in first.document_text
    assert calls["n"] == 1

    second = enrich(o, cache=cache)  # cache hit -> no refetch
    assert calls["n"] == 1
    assert second.document_text == first.document_text


def test_enrich_no_urls_is_noop():
    o = opp()
    assert enrich(o).document_text is None


# --------------------------------------------------------------------------- #
# the PDF actually drives a match
# --------------------------------------------------------------------------- #


def test_keyword_only_in_pdf_drives_a_match():
    profile = Profile(name="p", keywords=["domotica"])
    bare = opp()  # "domotica" appears nowhere in title/summary/eligibility
    assert prefilter([bare], profile, now=NOW) == []  # no signal -> dropped

    enriched = bare.model_copy(update={"document_text": extract_pdf_text(PDF_BYTES)})
    kept = prefilter([enriched], profile, now=NOW)
    assert [o.id for o in kept] == ["x:1"]  # PDF text now supplies the signal


# --------------------------------------------------------------------------- #
# SqliteDocumentCache
# --------------------------------------------------------------------------- #


def test_sqlite_document_cache_roundtrip(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    cache = SqliteDocumentCache(store)
    try:
        assert cache.get("https://x/doc.pdf") is None
        cache.set("https://x/doc.pdf", "hello domotica")
        assert cache.get("https://x/doc.pdf") == "hello domotica"
        cache.set("https://x/doc.pdf", "")  # empty cached too (no re-fetch)
        assert cache.get("https://x/doc.pdf") == ""
    finally:
        store.close()
