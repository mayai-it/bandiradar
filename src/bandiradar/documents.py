"""Optional document enrichment — fetch attachment PDFs and extract their text.

Open-core, OPTIONAL, injected like the score/benchmark caches. The default
install is light: ``pypdf`` only. OCR for scanned PDFs is the optional ``ocr``
extra (``pytesseract`` + ``pdf2image`` + the ``tesseract``/``poppler`` system
binaries); when it isn't available we skip OCR gracefully. Nothing here ever
raises into the matcher — failures degrade to empty text.
"""

from __future__ import annotations

import io
import logging
from typing import Protocol, runtime_checkable

import httpx

from bandiradar.models import Opportunity

logger = logging.getLogger(__name__)

_MIN_TEXT_CHARS = 40  # below this a PDF is treated as scanned -> try OCR
_FETCH_TIMEOUT = 30.0


def _extract_with_pypdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 — a bad page must not sink the doc
                continue
        return "\n".join(parts).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pypdf extraction failed: %s", exc)
        return ""


def _ocr_pdf(data: bytes) -> str:
    """OCR fallback. Returns "" unless the optional ``ocr`` extra + binaries exist."""
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
    except Exception:  # noqa: BLE001 — extra not installed -> skip cleanly
        return ""
    try:
        images = convert_from_bytes(data)
        return "\n".join(
            pytesseract.image_to_string(image, lang="ita") for image in images
        ).strip()
    except Exception as exc:  # noqa: BLE001 — system binaries missing -> skip cleanly
        logger.warning("OCR unavailable/failed: %s", exc)
        return ""


def extract_pdf_text(data: bytes) -> str:
    """PURE: extract text from PDF bytes (pypdf), OCR fallback if it looks scanned."""
    text = _extract_with_pypdf(data)
    if len(text) >= _MIN_TEXT_CHARS:
        return text
    ocr = _ocr_pdf(data)
    return ocr if len(ocr) > len(text) else text


def fetch_and_extract(url: str, client: httpx.Client | None = None) -> str:
    """GET ``url`` and extract PDF text. Non-PDF / HTTP errors -> "" (never raises)."""
    owns_client = client is None
    client = client or httpx.Client(timeout=_FETCH_TIMEOUT, follow_redirects=True)
    try:
        response = client.get(url)
        response.raise_for_status()
        data = response.content
        content_type = response.headers.get("content-type", "").lower()
        if "pdf" not in content_type and not data[:5].startswith(b"%PDF"):
            return ""
        return extract_pdf_text(data)
    except Exception as exc:  # noqa: BLE001 — enrichment must never break matching
        logger.warning("document fetch/extract failed for %s: %s", url, exc)
        return ""
    finally:
        if owns_client:
            client.close()


@runtime_checkable
class DocumentCache(Protocol):
    """Cache of extracted document text, keyed by URL."""

    def get(self, url: str) -> str | None: ...

    def set(self, url: str, text: str) -> None: ...


class InMemoryDocumentCache:
    """Default process-local cache (storage.SqliteDocumentCache persists)."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, url: str) -> str | None:
        return self._store.get(url)

    def set(self, url: str, text: str) -> None:
        self._store[url] = text


def enrich(
    opportunity: Opportunity,
    cache: DocumentCache | None = None,
    client: httpx.Client | None = None,
    max_docs: int = 3,
) -> Opportunity:
    """Fetch+extract the opportunity's document_urls into a document_text COPY.

    Cached per URL (get-or-fetch). Per-document failures are skipped. Returns the
    opportunity unchanged when it has no document_urls or nothing was extracted.
    """
    urls = opportunity.document_urls[:max_docs]
    if not urls:
        return opportunity

    texts: list[str] = []
    for url in urls:
        text = cache.get(url) if cache is not None else None
        if text is None:
            text = fetch_and_extract(url, client=client)
            if cache is not None:
                cache.set(url, text)  # cache "" too, so failures aren't re-fetched
        if text:
            texts.append(text)

    if not texts:
        return opportunity
    return opportunity.model_copy(update={"document_text": "\n\n".join(texts)})
