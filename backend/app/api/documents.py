from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from app.services.document_reader import read_document, read_url

router = APIRouter(prefix="/api/documents", tags=["documents"])


class UrlIn(BaseModel):
    url: str


@router.post("/read")
async def read_upload(file: UploadFile) -> dict:
    data = await file.read()
    return read_document(file.filename or "upload", data).to_dict()


@router.post("/read-url")
def read_url_endpoint(req: UrlIn) -> dict:
    try:
        return read_url(req.url).to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
