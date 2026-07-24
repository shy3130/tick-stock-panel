from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "recovery" / "production-1605-backend.json"
RECOVERED_COMMIT = "23a2ae4eda7fecae26ecb14275f536fb7eb58531"


def _recovered_sha256(relative: str) -> str:
    blob = subprocess.run(
        ["git", "show", f"{RECOVERED_COMMIT}:{relative}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(blob).hexdigest()


def test_recovered_backend_matches_authoritative_image_manifest() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert payload["imageId"] == (
        "sha256:fcf690148cb121e4abf328ae8d38a90a39ee83c9ef3ae4bf3d8e298348d2793a"
    )
    mismatches = {
        relative: (_recovered_sha256(relative), expected)
        for relative, expected in payload["files"].items()
        if _recovered_sha256(relative) != expected
    }
    assert mismatches == {}
