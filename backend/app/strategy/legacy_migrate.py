"""去内置化迁移 — 旧数据目录的一次性自动清理。

内置策略移除后, 老环境 data/strategies/ 里的部分用户文件加载失败
(composite 引用的内置子策略不存在、import 已删除的内置模块)。策略页
会逐文件报错刷屏。本模块在启动时把「不可恢复」的失败文件移入归档
目录 (引擎只扫顶层 *.py, 子目录不参与加载), 让报错消失、目录干净;
归档不删除, 用户可随时找回。

分类保守: 只归档明确由「内置世界残留」引起的失败, 语法错误等普通
失败仍走原有 load_errors 提示, 绝不误归档用户编辑中的文件。
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

ARCHIVE_DIRNAME = "_archive_unloadable"

# 归档条件: 错误信息命中任一模式 = 「内置残留」类失败
_ARCHIVE_PATTERNS = (
    re.compile(r"app\.strategy\.builtin"),          # import 已删除的内置模块 (垫片之外的)
    re.compile(r"子策略.*不存在"),                    # composite 断链
    re.compile(r"composite strategy.*不存在"),
    re.compile(r"unknown strategy"),                 # META/引用指向不存在的策略
)


def is_legacy_unloadable(error: str) -> bool:
    """错误信息是否属于「内置残留」类不可恢复失败。"""
    return any(p.search(error) for p in _ARCHIVE_PATTERNS)


def archive_unloadable_strategies(load_errors: list[dict]) -> list[str]:
    """把内置残留类失败文件移入 data/strategies/<dir>/_archive_unloadable/。

    load_errors 为引擎 load_errors() 的 [{file, error}]; 返回被归档的
    文件名列表 (空 = 无需归档)。文件缺失/移动失败时跳过并告警, 不抛错。
    """
    archived: list[str] = []
    for item in load_errors:
        f = Path(str(item.get("file", "")))
        error = str(item.get("error", ""))
        if not f.exists() or not is_legacy_unloadable(error):
            continue
        # 只处理 data/strategies/ 下的文件 (研究模板等代码文件不动)
        if "strategies" not in f.parts:
            continue
        try:
            dest_dir = f.parent / ARCHIVE_DIRNAME
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f.name
            # 同名碰撞: 追加序号, 不覆盖既有归档
            i = 1
            while dest.exists():
                dest = dest_dir / f"{f.stem}_{i}{f.suffix}"
                i += 1
            shutil.move(str(f), str(dest))
            archived.append(f.name)
            logger.info("legacy migrate: 归档失效策略文件 %s (%s)", f.name, error[:120])
        except OSError as e:
            logger.warning("legacy migrate: 归档 %s 失败: %s", f, e)
    return archived
