from __future__ import annotations

import csv
import ipaddress
import socket
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from io import BytesIO, StringIO
from urllib.parse import urlparse

import httpx
import polars as pl

MAX_BYTES = 5 * 1024 * 1024
MAX_CHARS = 20_000
TABLE_PREVIEW_ROWS = 50


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
    warnings = []
    if len(data) > MAX_BYTES:
        data = data[:MAX_BYTES]
        warnings.append("file truncated to max bytes")
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
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
        text = ""
        kind = "pdf"
        warnings.append("pdf text extraction unsupported")
    else:
        text = data.decode("utf-8", errors="replace")
        kind = "text"
        warnings.append(f"unsupported extension treated as text: {suffix}")
    text, truncated = _truncate(text, warnings)
    return DocumentEnvelope(filename, kind, filename, text, len(text), truncated, warnings)


def read_url(url: str) -> DocumentEnvelope:
    _validate_public_url(url)
    with httpx.Client(timeout=10.0, follow_redirects=True, trust_env=False) as client:
        resp = client.get(url)
    resp.raise_for_status()
    final_url = str(resp.url)
    _validate_public_url(final_url)
    content_type = resp.headers.get("content-type", "")
    text = _html_to_text(resp.text) if "html" in content_type.lower() else resp.text
    warnings: list[str] = []
    text, truncated = _truncate(text, warnings)
    return DocumentEnvelope(final_url, "html" if "html" in content_type.lower() else "text", final_url, text, len(text), truncated, warnings)


def _truncate(text: str, warnings: list[str]) -> tuple[str, bool]:
    if len(text) <= MAX_CHARS:
        return text, False
    warnings.append("text truncated to max chars")
    return text[:MAX_CHARS], True


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
