from __future__ import annotations

import csv
import ipaddress
import socket
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from io import BytesIO, StringIO
from urllib.parse import urljoin, urlparse

import httpx
import polars as pl

MAX_BYTES = 5 * 1024 * 1024
MAX_CHARS = 20_000
MAX_PROMPT_DOCUMENT_CHARS = 20_000
TABLE_PREVIEW_ROWS = 50
MAX_PDF_PAGES = 200


@dataclass
class DocumentEnvelope:
    source: str
    kind: str
    title: str
    text: str
    char_count: int
    truncated: bool
    warnings: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def read_document(filename: str, data: bytes) -> DocumentEnvelope:
    warnings: list[str] = []
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
    input_truncated = False
    if len(data) > MAX_BYTES:
        input_truncated = True
        if suffix == "pdf":
            warnings.append("pdf exceeds max bytes; extraction skipped")
            return DocumentEnvelope(filename, "pdf", filename, "", 0, True, warnings)
        data = data[:MAX_BYTES]
        warnings.append("file truncated to max bytes")
    if suffix in {"txt", "md"}:
        text = data.decode("utf-8", errors="replace")
        kind = "markdown" if suffix == "md" else "text"
    elif suffix == "csv":
        text = _csv_preview(data)
        kind = "csv"
    elif suffix in {"xlsx", "xls"}:
        text = _xlsx_preview(data)
        kind = "xlsx"
    elif suffix == "pdf":
        kind = "pdf"
        if not data.startswith(b"%PDF-"):
            text = ""
            warnings.append("declared .pdf but missing PDF magic header")
        else:
            text = _extract_pdf_text(data, warnings)
    else:
        text = data.decode("utf-8", errors="replace")
        kind = "text"
        warnings.append(f"unsupported extension treated as text: {suffix}")
    text, text_truncated = _truncate(text, warnings)
    return DocumentEnvelope(
        filename,
        kind,
        filename,
        text,
        len(text),
        input_truncated or text_truncated,
        warnings,
    )


def read_url(url: str) -> DocumentEnvelope:
    resp = _fetch_public_url(url)
    resp.raise_for_status()
    final_url = str(resp.url)
    content_type = resp.headers.get("content-type", "")
    text = _html_to_text(resp.text) if "html" in content_type.lower() else resp.text
    warnings: list[str] = []
    text, truncated = _truncate(text, warnings)
    return DocumentEnvelope(final_url, "html" if "html" in content_type.lower() else "text", final_url, text, len(text), truncated, warnings)


def format_prompt_document(document_text: str = "") -> str:
    text = document_text.strip()
    if not text:
        return ""
    if len(text) > MAX_PROMPT_DOCUMENT_CHARS:
        text = text[:MAX_PROMPT_DOCUMENT_CHARS]
    return "## 用户附件摘要（非行情事实）\n" + text


def _fetch_public_url(url: str, max_redirects: int = 5):
    current = url
    with httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False) as client:
        for _ in range(max_redirects + 1):
            _validate_public_url(current)
            resp = client.get(current)
            if resp.status_code not in {301, 302, 303, 307, 308}:
                return resp
            location = resp.headers.get("location")
            if not location:
                return resp
            current = urljoin(current, location)
        raise ValueError("too many redirects")


def _truncate(text: str, warnings: list[str]) -> tuple[str, bool]:
    if len(text) <= MAX_CHARS:
        return text, False
    warnings.append("text truncated to max chars")
    return text[:MAX_CHARS], True


def _import_pdfium():
    import pypdfium2 as pdfium

    return pdfium


def _extract_pdf_text(data: bytes, warnings: list[str]) -> str:
    """Extract a bounded PDF text layer entirely in memory.

    Image-only pages are reported but never OCRed. The uploaded bytes are not
    persisted, and extraction stops once the prompt text budget is exceeded.
    """
    try:
        pdfium = _import_pdfium()
    except ImportError:
        warnings.append("pdf extraction unavailable: install pypdfium2")
        return ""

    chunks: list[str] = []
    empty_pages: list[int] = []
    try:
        with pdfium.PdfDocument(data) as document:
            total_pages = len(document)
            page_count = min(total_pages, MAX_PDF_PAGES)
            if total_pages > MAX_PDF_PAGES:
                warnings.append(
                    f"pdf page limit applied: read {MAX_PDF_PAGES} of {total_pages} pages"
                )
            if total_pages == 0:
                warnings.append("pdf has no pages")
                return ""

            extracted_chars = 0
            for page_index in range(page_count):
                try:
                    page = document[page_index]
                    try:
                        text_page = page.get_textpage()
                        try:
                            page_text = text_page.get_text_bounded().strip()
                        finally:
                            text_page.close()
                    finally:
                        page.close()
                except Exception as exc:  # noqa: BLE001
                    warnings.append(
                        f"pdf page {page_index + 1} text extraction failed: "
                        f"{type(exc).__name__}"
                    )
                    continue

                if not page_text:
                    empty_pages.append(page_index + 1)
                    continue
                chunk = f"--- Page {page_index + 1} ---\n{page_text}"
                chunks.append(chunk)
                extracted_chars += len(chunk) + 2
                if extracted_chars > MAX_CHARS:
                    break
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"pdf text extraction failed: {type(exc).__name__}")
        return ""

    if empty_pages:
        preview = ",".join(str(page) for page in empty_pages[:20])
        suffix = f" (+{len(empty_pages) - 20} more)" if len(empty_pages) > 20 else ""
        warnings.append(
            f"pdf pages with no extractable text (OCR disabled): {preview}{suffix}"
        )
    return "\n\n".join(chunks)


def _csv_preview(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    rows = list(csv.reader(StringIO(text)))[:TABLE_PREVIEW_ROWS]
    if not rows:
        return "row_count=0"
    return _markdown_table(rows) + f"\n\nrow_count_preview={len(rows)}"


def _xlsx_preview(data: bytes) -> str:
    df = pl.read_excel(BytesIO(data))
    return _df_preview(df)


def _df_preview(df: pl.DataFrame) -> str:
    rows = [df.columns] + [[str(v) if v is not None else "" for v in row] for row in df.head(TABLE_PREVIEW_ROWS).iter_rows()]
    return _markdown_table(rows) + f"\n\nrow_count_preview={min(df.height, TABLE_PREVIEW_ROWS)}"


def _markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    header = "| " + " | ".join(map(str, padded[0])) + " |"
    sep = "| " + " | ".join(["---"] * width) + " |"
    body = ["| " + " | ".join(map(str, r)) + " |" for r in padded[1:]]
    return "\n".join([header, sep, *body])


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only public http/https urls are supported")
    for info in socket.getaddrinfo(parsed.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            raise ValueError("private urls are not allowed")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.skip = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ARG002
        if tag in {"script", "style"}:
            self.skip = True

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = False

    def handle_data(self, data):
        if not self.skip:
            s = " ".join(data.split())
            if s:
                self.parts.append(s)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.parts)
