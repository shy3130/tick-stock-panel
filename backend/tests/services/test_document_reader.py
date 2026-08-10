import socket

import polars as pl
import pytest

from app.services import document_reader as dr


def _pdf_bytes(text: str | None = "Hello PDF text layer") -> bytes:
    stream = (
        f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        if text is not None
        else b""
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode())
        body.extend(obj)
        body.extend(b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(body)


def test_read_txt_truncates(monkeypatch):
    monkeypatch.setattr(dr, "MAX_CHARS", 5)

    out = dr.read_document("a.txt", b"abcdef")

    assert out.text == "abcde"
    assert out.truncated is True
    assert out.warnings


def test_read_csv_preview():
    out = dr.read_document("a.csv", b"name,value\nA,1\n")

    assert out.kind == "csv"
    assert "| name | value |" in out.text
    assert "row_count_preview=2" in out.text


def test_read_xlsx_uses_polars(monkeypatch):
    monkeypatch.setattr(dr.pl, "read_excel", lambda _: pl.DataFrame({"a": [1], "b": ["x"]}))

    out = dr.read_document("a.xlsx", b"fake")

    assert out.kind == "xlsx"
    assert "| a | b |" in out.text


@pytest.mark.parametrize("url", ["file:///x", "http://127.0.0.1/a", "http://10.0.0.1/a"])
def test_reject_private_url(url, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))])

    with pytest.raises(ValueError):
        dr.read_url(url)


def test_fetch_url_uses_trust_env_false(monkeypatch):
    seen = {}
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))])

    class Resp:
        url = "https://example.com/a"
        status_code = 200
        text = "<html><script>x</script><body>Hello <b>world</b></body></html>"
        headers = {"content-type": "text/html"}

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):  # noqa: ARG002
            return Resp()

    monkeypatch.setattr(dr.httpx, "Client", Client)

    out = dr.read_url("https://example.com/a")

    assert seen["trust_env"] is False
    assert seen["follow_redirects"] is False
    assert "Hello" in out.text
    assert "x" not in out.text


def test_redirect_to_private_url_rejected_before_second_request(monkeypatch):
    calls = []

    def fake_addr(host, *args):
        ip = "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_addr)

    class Resp:
        url = "https://example.com/a"
        status_code = 302
        headers = {"location": "http://127.0.0.1/admin"}
        text = ""

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            calls.append(url)
            return Resp()

    monkeypatch.setattr(dr.httpx, "Client", Client)

    with pytest.raises(ValueError):
        dr.read_url("https://example.com/a")

    assert calls == ["https://example.com/a"]



def test_read_pdf_extracts_text_layer():
    out = dr.read_document("report.pdf", _pdf_bytes())

    assert out.kind == "pdf"
    assert "Hello PDF text layer" in out.text
    assert out.char_count == len(out.text)
    assert out.truncated is False
    assert not any("unsupported" in warning for warning in out.warnings)


def test_read_pdf_reports_image_only_page():
    out = dr.read_document("scan.pdf", _pdf_bytes(None))

    assert out.kind == "pdf"
    assert out.text == ""
    assert any("no extractable text" in warning for warning in out.warnings)


def test_read_pdf_rejects_magic_mismatch():
    out = dr.read_document("fake.pdf", b"not a pdf")

    assert out.kind == "pdf"
    assert out.text == ""
    assert any("magic header" in warning for warning in out.warnings)


def test_read_pdf_skips_oversized_input(monkeypatch):
    monkeypatch.setattr(dr, "MAX_BYTES", 8)

    out = dr.read_document("large.pdf", b"%PDF-" + b"x" * 20)

    assert out.kind == "pdf"
    assert out.text == ""
    assert out.truncated is True
    assert any("exceeds max bytes" in warning for warning in out.warnings)


def test_read_pdf_missing_dependency_is_fail_soft(monkeypatch):
    def missing():
        raise ImportError("missing")

    monkeypatch.setattr(dr, "_import_pdfium", missing)

    out = dr.read_document("report.pdf", _pdf_bytes())

    assert out.kind == "pdf"
    assert out.text == ""
    assert any("extraction unavailable" in warning for warning in out.warnings)


def test_read_pdf_applies_page_limit(monkeypatch):
    monkeypatch.setattr(dr, "MAX_PDF_PAGES", 0)

    out = dr.read_document("report.pdf", _pdf_bytes())

    assert out.text == ""
    assert any("page limit applied" in warning for warning in out.warnings)