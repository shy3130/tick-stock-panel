"""跨平台文件替换辅助函数。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def replace_with_retry(
    source: Path,
    target: Path,
    *,
    attempts: int = 8,
    initial_delay: float = 0.05,
    max_delay: float = 0.5,
) -> None:
    """原子替换文件,并重试 Windows 上短暂的拒绝访问。

    DuckDB、Polars、杀毒软件或前端状态查询可能极短暂地持有 parquet 句柄。
    仅重试 ``PermissionError``; 目录、磁盘等其他错误立即上抛,避免掩盖真故障。
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    source = Path(source)
    target = Path(target)
    for attempt in range(attempts):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            delay = min(initial_delay * (2**attempt), max_delay)
            logger.warning(
                "文件暂时被占用,%.2f 秒后重试原子替换 (%d/%d): %s",
                delay,
                attempt + 1,
                attempts,
                target,
            )
            time.sleep(delay)
