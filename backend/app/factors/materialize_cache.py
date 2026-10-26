"""L2 内容寻址因子列缓存 (P2b) — 自定义 (DSL) 因子物化结果的复用。

键 = sha256(因子定义指纹 + 全部依赖列内容指纹 + 行数)。内容指纹即失效
传播: 公式变 / 依赖列任何值变 → 键变 → 自然失效, 不需要数据版本号或
mtime 签名 — 键本身覆盖「同一因子作用在同一输入上」的全部语义。

fail-open: 缓存读写任何故障 (目录不可写 / parquet 损坏 / 超限) 都回退
现算。缓存是纯优化, 永不影响正确性; 目录可整删 (内容寻址, 删除安全)。

两层:
  - 进程内 LRU (键 → 列, 字节上限): 同帧/同数据的重复求值近零成本
    (守护复验、优化器、实验室批量回测);
  - 磁盘 parquet (data/cache/factor_columns/<key>.parquet, 文件数上限):
    跨进程复用 (回测 worker 每任务一个进程)。

列内容指纹按 (frame, 列名) 记忆 — 同一帧上 K 个因子共享依赖列, 每列只
哈希一次 (hash+sha256 ≈ 8ms/百万行, 远低于因子现算)。frame 指纹表用
weakref 弱持有, 帧被回收后自动失效, 不阻止内存释放。
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from app.config import settings

logger = logging.getLogger(__name__)

# 小帧不缓存: 哈希+IO 比现算还贵 (筛选页预览、单标的明细等)。
_MIN_ROWS = 50_000
_LRU_MAX_BYTES = 256 * 1024 * 1024
_DISK_MAX_FILES = 1000
_DISK_SUBDIR = Path("cache") / "factor_columns"


@dataclass
class _Entry:
    series: pl.Series
    nbytes: int


_lru: OrderedDict[str, _Entry] = OrderedDict()
_lru_bytes = 0
# (id(frame) → {列名: 指纹}); finalize 在帧被 GC 时清除条目 — 不强持帧引用,
# id 也不会复用到陈旧条目 (条目随帧消亡)。
_frame_digests: dict[int, dict[str, str]] = {}
_stats = {"lru_hit": 0, "disk_hit": 0, "miss": 0, "store": 0, "bypass_small": 0, "error": 0}
_lock = threading.Lock()


def stats() -> dict[str, int]:
    """缓存命中计数 (观测/测试用)。"""
    return dict(_stats)


def invalidate(*, disk: bool = False) -> None:
    """清空进程内 LRU 并重置命中计数; disk=True 时连磁盘缓存目录一起清
    (内容寻址, 整删安全)。"""
    global _lru_bytes
    with _lock:
        _lru.clear()
        _lru_bytes = 0
        _frame_digests.clear()
    _stats.update({"lru_hit": 0, "disk_hit": 0, "miss": 0, "store": 0, "bypass_small": 0, "error": 0})
    if disk:
        import shutil

        root = Path(settings.data_dir) / _DISK_SUBDIR
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)


def _column_digest(frame: pl.DataFrame, column: str) -> str:
    """单列内容指纹 (同帧记忆化): dtype + null 数 + 逐行 murmur 哈希的 sha256。

    Series.hash 跨进程确定 (同 polars 版本, 固定种子), 行序敏感 — 顺序不同
    视为不同输入 (物化按行位对齐, 顺序本来就是语义的一部分)。"""
    with _lock:
        frame_key = id(frame)
        memo = _frame_digests.get(frame_key)
        if memo is None:
            memo = {}
            _frame_digests[frame_key] = memo
            weakref.finalize(frame, _frame_digests.pop, frame_key, None)
        elif column in memo:
            return memo[column]
    series = frame.get_column(column)
    digest = hashlib.sha256()
    digest.update(column.encode())
    digest.update(str(series.dtype).encode())
    digest.update(f"nulls={series.null_count()};rows={series.len()};".encode())
    digest.update(series.hash().to_numpy().tobytes())
    value = digest.hexdigest()
    with _lock:
        memo[column] = value
    return value


def cache_key(
    factor_id: str,
    formula_text: str,
    version: int,
    frame: pl.DataFrame,
    dependency_columns,
) -> str | None:
    """定义 + 输入内容 → 缓存键; None = 不缓存 (小帧 / 依赖列缺一)。"""
    if frame.height < _MIN_ROWS:
        _stats["bypass_small"] += 1
        return None
    columns = sorted(set(dependency_columns))
    digests: list[str] = []
    for column in columns:
        if column not in frame.columns:
            return None  # 缺列时 frame_transform 本就会 E013 拒绝, 无缓存意义
        digests.append(_column_digest(frame, column))
    digest = hashlib.sha256()
    digest.update(f"{factor_id}|v{version}|{formula_text}|rows={frame.height};".encode())
    digest.update(";".join(digests).encode())
    return digest.hexdigest()


def _disk_path(key: str) -> Path:
    return Path(settings.data_dir) / _DISK_SUBDIR / f"{key}.parquet"


def get_cached(key: str, expected_rows: int) -> pl.Series | None:
    """取缓存的因子列; LRU 未命中读磁盘并晋升; 任何异常按未命中处理。"""
    global _lru_bytes
    with _lock:
        entry = _lru.get(key)
        if entry is not None:
            _lru.move_to_end(key)
            if entry.series.len() == expected_rows:
                _stats["lru_hit"] += 1
                return entry.series
            _lru.pop(key)
            _lru_bytes -= entry.nbytes
    path = _disk_path(key)
    if not path.exists():
        _stats["miss"] += 1
        return None
    try:
        series = pl.read_parquet(path).to_series(0)
        if series.len() != expected_rows:
            raise ValueError(f"缓存行数不符: {series.len()} != {expected_rows}")
    except Exception as e:  # noqa: BLE001 — 损坏缓存删除并回退现算
        _stats["error"] += 1
        logger.debug("factor cache read failed %s: %s", key[:12], e)
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        _stats["miss"] += 1
        return None
    _stats["disk_hit"] += 1
    put_cached(key, series)
    return series


def put_cached(key: str, series: pl.Series) -> None:
    """写入两层缓存; 超限/故障静默跳过 (fail-open)。"""
    global _lru_bytes
    nbytes = series.len() * 8  # 物化因子列均为 float64
    with _lock:
        if key not in _lru:
            _lru[key] = _Entry(series=series, nbytes=nbytes)
            _lru_bytes += nbytes
            while _lru_bytes > _LRU_MAX_BYTES and len(_lru) > 1:
                _, evicted = _lru.popitem(last=False)
                _lru_bytes -= evicted.nbytes
            _stats["store"] += 1
    try:
        root = _disk_path(key).parent
        if sum(1 for _ in root.glob("*.parquet")) >= _DISK_MAX_FILES:
            return  # 文件数上限: 跳写不淘汰 (目录可整删, 见模块 docstring)
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / f"{key}.parquet.tmp"
        series.to_frame().write_parquet(tmp)
        tmp.replace(root / f"{key}.parquet")
    except OSError as e:
        _stats["error"] += 1
        logger.debug("factor cache write failed %s: %s", key[:12], e)
