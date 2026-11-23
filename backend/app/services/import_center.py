"""Identify import types, then delegate to their existing validation services."""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

from app.data_providers.custom import packages as source_packages
from app.extensions.packages import ExtensionPackageStore
from app.services import chart_indicators, config_backup, resource_bundle
from app.services.engine_package_store import EnginePackageStore
from app.services.engine_releases import MARKER


def inspect_uploads(uploads: list[tuple[str, bytes]], data_dir: Path) -> dict:
    if not uploads:
        raise ValueError("请选择要导入的文件")
    kinds = []
    for filename, data in uploads:
        suffix = Path(filename).suffix.lower()
        kind = "resources"
        if suffix in (".js", ".mjs", ".tsx"):
            kind = "page"
        elif suffix == ".zip":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    if len(archive.infolist()) > config_backup.MAX_ENTRIES:
                        raise ValueError("包内文件数量超过上限")
                    names = archive.namelist()
                    if sum(marker in names for marker in ("extension.yaml", "plugin.yaml", MARKER)) > 1:
                        raise ValueError("扩展包类型不明确，不能同时包含两种安装清单")
                    if MARKER in names:
                        kind = "engine"
                    elif "extension.yaml" in names:
                        kind = "extension"
                    elif "plugin.yaml" in names:
                        kind = "source"
                    elif "manifest.json" in names:
                        info = archive.getinfo("manifest.json")
                        if info.file_size > 64 * 1024:
                            raise ValueError("包清单超过 64 KiB")
                        doc = json.loads(archive.read(info))
                        if isinstance(doc, dict) and doc.get("format") == "tick-config-backup":
                            kind = "backup"
            except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("无法识别 ZIP 文件或包清单") from exc
        elif suffix == ".json":
            try:
                doc = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("JSON 文件无效") from exc
            if isinstance(doc, dict) and doc.get("format") == "tick-chart-indicators":
                kind = "chart_indicators"
        elif suffix != ".py":
            raise ValueError("支持 ZIP、策略 .py、定义 .json 和页面 .js/.mjs/.tsx")
        kinds.append(kind)
    if len(uploads) > 1 and any(kind != "resources" for kind in kinds):
        raise ValueError("策略、信号和因子可以多选；指标包、备份、数据源和扩展包请逐包导入")
    kind = kinds[0]
    filename, data = uploads[0]
    if kind == "engine":
        preview = EnginePackageStore(data_dir).inspect(data)
    elif kind == "extension":
        preview = ExtensionPackageStore(data_dir).inspect(data)
    elif kind == "source":
        preview = source_packages.inspect_archive(data)[0]
    elif kind == "backup":
        preview = config_backup.inspect_archive(data)
    elif kind == "chart_indicators":
        preview = chart_indicators.inspect_definitions(data)
    elif kind == "page":
        limit = (1 if filename.lower().endswith(".tsx") else 5) * 1024 * 1024
        if not data or len(data) > limit:
            raise ValueError(f"页面文件为空或超过 {limit // 1024 // 1024} MiB")
        data.decode("utf-8")
        preview = {
            "filename": filename, "size": len(data), "digest": hashlib.sha256(data).hexdigest(),
            "source": filename.lower().endswith(".tsx"),
            "suggested_id": re.sub(r"[^a-zA-Z0-9_]", "_", Path(filename).stem)[:64] or "imported_page",
        }
    else:
        preview = resource_bundle.inspect_resources(uploads)
    return {"kind": kind, "preview": preview}
