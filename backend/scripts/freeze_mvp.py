"""Freeze or verify the immutable TickFlow MVP v0.1.0 snapshot."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from research.alphagpt.release import sha256_file
from research.paths import ARCHIVE_ARTIFACTS_DIR, CURRENT_ARTIFACTS_DIR, PROJECT_ROOT

RELEASE_VERSION = "0.1.0"
RELEASE_DATE = "2026-08-02"
ARCHIVE_DIR = ARCHIVE_ARTIFACTS_DIR / "mvp" / f"v{RELEASE_VERSION}"
MANIFEST_NAME = "manifest.json"
NOTES_NAME = "RELEASE.md"

RESULT_FILES = (
    ("mvp_backtest.json", "deterministic machine-readable MVP result"),
    ("mvp_backtest.html", "offline human-readable MVP report"),
)

SOURCE_FILES = (
    "backend/scripts/run_mvp.py",
    "backend/app/config.py",
    "backend/app/main.py",
    "backend/app/backtest/engine.py",
    "backend/app/backtest/strategy.py",
    "backend/app/backtest/worker.py",
    "backend/app/strategy/builtin/trend_breakout.py",
    "backend/research/common/universe.py",
    "backend/research/paths.py",
    "dev.ps1",
    "dev.sh",
    ".env.example",
    "README.md",
)


def _record(path: Path, *, relative_to: Path, role: str) -> dict[str, Any]:
    return {
        "file": path.relative_to(relative_to).as_posix(),
        "role": role,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _copy_immutable(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"冻结必需文件不存在: {source}")
    if target.exists():
        if not target.is_file() or sha256_file(source) != sha256_file(target):
            raise ValueError(f"拒绝覆盖已有冻结文件: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(target)


def _write_immutable(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    if path.exists():
        if not path.is_file() or path.read_bytes() != encoded:
            raise ValueError(f"拒绝覆盖已有冻结清单: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def _assert_finite(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_finite(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_finite(item, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"冻结结果包含 NaN/Infinity: {path}")


def _load_result(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    _assert_finite(payload)
    if payload.get("schema") != "tickflow.mvp_backtest.v1":
        raise ValueError("MVP 结果 schema 不匹配")
    if payload.get("strategy", {}).get("id") != "trend_breakout":
        raise ValueError("MVP v0.1.0 只允许冻结 trend_breakout")
    if payload.get("result", {}).get("status") != "completed":
        raise ValueError("只能冻结成功完成的 MVP 回测")
    if not payload.get("data_status", {}).get("valid"):
        raise ValueError("只能冻结通过数据质量门的 MVP 回测")
    return payload


def _git_state(project_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(project_root), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    try:
        status = run("status", "--porcelain")
        return {
            "head": run("rev-parse", "HEAD"),
            "branch": run("branch", "--show-current"),
            "working_tree_dirty": bool(status),
            "note": (
                "完整仓库源码尚未 Git 冻结；本发布归档保存 MVP 入口及关键依赖快照。"
                if status
                else "working tree clean at freeze time"
            ),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"head": None, "branch": None, "working_tree_dirty": None, "note": "git unavailable"}


def _render_notes(manifest: Mapping[str, Any]) -> str:
    metrics = manifest["result_summary"]["metrics"]
    data = manifest["result_summary"]["data"]
    artifacts = "\n".join(
        f"| `{record['file']}` | `{record['sha256']}` |"
        for record in manifest["artifacts"]
    )
    return f"""# TickFlow MVP v{RELEASE_VERSION}

冻结状态：**功能闭环已冻结，策略未晋级，不可解释为生产 alpha。**

## 冻结结果

- 策略：趋势突破（`trend_breakout`）
- 数据区间：{data['min_date']} 至 {data['max_date']}
- 策略收益：{metrics.get('total_return', 0):+.2%}
- 上证基准：{metrics.get('benchmark_return', 0):+.2%}
- 超额收益：{metrics.get('excess', 0):+.2%}
- 最大回撤：{metrics.get('max_drawdown', 0):+.2%}
- 交易数：{metrics.get('n_trades', 0)}
- 协议哈希：`{manifest['result_summary']['protocol_hash']}`

## 边界

- 包含：无前端 CLI、数据质量门、固定股票池、真实 Matrix 回测、JSON/HTML 报告。
- 不包含：盈利承诺、实盘交易、自动牛熊切换、AlphaGPT/PPO 调优。
- 当前工作区在冻结时仍有未提交改动，因此这是**产物与关键源码快照冻结**，不是 Git tag。

## 验证

```powershell
Set-Location backend
.\\.venv\\Scripts\\python.exe -m scripts.freeze_mvp --verify-only
```

## 冻结文件

| 文件 | SHA-256 |
|---|---|
{artifacts}
"""


def freeze_release(
    *,
    current_dir: Path = CURRENT_ARTIFACTS_DIR,
    archive_dir: Path = ARCHIVE_DIR,
    project_root: Path = PROJECT_ROOT,
    source_files: Sequence[str] = SOURCE_FILES,
) -> dict[str, Any]:
    result_payload = _load_result(current_dir / "mvp_backtest.json")

    for filename, _role in RESULT_FILES:
        _copy_immutable(current_dir / filename, archive_dir / filename)
    for relative in source_files:
        _copy_immutable(project_root / relative, archive_dir / "source" / relative)

    artifact_records = [
        _record(archive_dir / filename, relative_to=archive_dir, role=role)
        for filename, role in RESULT_FILES
    ]
    artifact_records.extend(
        _record(
            archive_dir / "source" / relative,
            relative_to=archive_dir,
            role="MVP source snapshot",
        )
        for relative in source_files
    )
    manifest = {
        "schema": "tickflow.mvp_release.v1",
        "release": {
            "version": RELEASE_VERSION,
            "date": RELEASE_DATE,
            "status": "frozen_research_mvp_not_production_alpha",
        },
        "result_summary": {
            "protocol_hash": result_payload["protocol_hash"],
            "evidence_status": result_payload["evidence_status"],
            "strategy": result_payload["strategy"],
            "seed": result_payload["seed"],
            "universe_sha256": result_payload["universe"]["symbols_sha256"],
            "data": result_payload["data_status"],
            "metrics": result_payload["result"]["metrics"],
        },
        "git": _git_state(project_root),
        "artifacts": artifact_records,
        "verification": {
            "command": ".venv/Scripts/python.exe -m scripts.freeze_mvp --verify-only",
            "policy": "same version is idempotent; any differing frozen file is rejected",
        },
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _write_immutable(archive_dir / MANIFEST_NAME, manifest_text)
    _write_immutable(archive_dir / NOTES_NAME, _render_notes(manifest))
    return manifest


def verify_release(archive_dir: Path = ARCHIVE_DIR) -> dict[str, Any]:
    manifest_path = archive_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "tickflow.mvp_release.v1":
        raise ValueError("冻结清单 schema 不匹配")
    if manifest.get("release", {}).get("version") != RELEASE_VERSION:
        raise ValueError("冻结版本不匹配")
    failures: list[str] = []
    for record in manifest.get("artifacts", []):
        path = archive_dir / str(record["file"])
        if not path.is_file() or sha256_file(path) != record["sha256"]:
            failures.append(str(record["file"]))
    expected_notes = _render_notes(manifest).encode("utf-8")
    notes = archive_dir / NOTES_NAME
    if not notes.is_file() or notes.read_bytes() != expected_notes:
        failures.append(NOTES_NAME)
    if failures:
        raise ValueError(f"冻结校验失败: {', '.join(failures)}")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = verify_release() if args.verify_only else freeze_release()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"MVP 冻结失败: {exc}")
        return 2
    print(f"MVP v{manifest['release']['version']} 冻结校验通过")
    print(f"目录: {ARCHIVE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
