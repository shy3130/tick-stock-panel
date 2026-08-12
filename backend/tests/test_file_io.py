"""Windows 原子替换的瞬时文件占用回归测试。"""
from __future__ import annotations

from pathlib import Path


def test_replace_with_retry_recovers_from_transient_permission_error(
    tmp_path,
    monkeypatch,
):
    from app import file_io

    source = tmp_path / "part.parquet.tmp"
    target = tmp_path / "part.parquet"
    source.write_bytes(b"new")
    target.write_bytes(b"old")

    original_replace = Path.replace
    attempts = []
    delays = []

    def flaky_replace(self, actual_target):
        attempts.append((self, Path(actual_target)))
        if len(attempts) < 3:
            raise PermissionError(13, "file is temporarily in use", str(actual_target))
        return original_replace(self, actual_target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr(file_io.time, "sleep", lambda delay: delays.append(delay))

    file_io.replace_with_retry(source, target)

    assert len(attempts) == 3
    assert delays == [0.05, 0.1]
    assert target.read_bytes() == b"new"
    assert not source.exists()
