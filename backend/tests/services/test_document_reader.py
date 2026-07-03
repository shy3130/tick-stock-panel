import socket

import polars as pl
import pytest

from app.services import document_reader as dr


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
