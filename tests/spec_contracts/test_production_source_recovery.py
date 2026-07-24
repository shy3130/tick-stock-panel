from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "recovery" / "production-1502-backend.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_recovered_backend_matches_authoritative_image_manifest() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert payload["imageId"] == (
        "sha256:de3adcadb363856453df72227ff92b69ada818f331c8ccfc4e3001b48f41a721"
    )
    mismatches = {
        relative: (_sha256(ROOT / relative), expected)
        for relative, expected in payload["files"].items()
        if _sha256(ROOT / relative) != expected
    }
    assert mismatches == {}
