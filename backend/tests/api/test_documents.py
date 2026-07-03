import asyncio

import pytest
from fastapi import HTTPException

from app.api import documents


class Upload:
    filename = "note.txt"

    async def read(self):
        return b"hello"


def test_read_upload_returns_envelope():
    out = asyncio.run(documents.read_upload(Upload()))

    assert out["kind"] == "text"
    assert out["text"] == "hello"


def test_read_url_maps_validation_error(monkeypatch):
    monkeypatch.setattr(documents, "read_url", lambda _: (_ for _ in ()).throw(ValueError("bad")))

    with pytest.raises(HTTPException) as exc:
        documents.read_url_endpoint(documents.UrlIn(url="http://127.0.0.1"))

    assert exc.value.status_code == 400
