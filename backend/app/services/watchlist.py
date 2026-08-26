"""自选股与分组服务(§6.1)。

自选存储于 ``{data_dir}/user_data/watchlist.parquet``, 分组定义存储于同目录的
``watchlist_groups.json``。

成员关系为多值 (M:N): 每条自选带 ``group_ids: list[str]``, 同一标的可同时
属于多个分组; 移出分组只摘标签(标的仍在自选), 移出自选才删除实体。

schema 兼容:
- 旧文件无组列 → 只读映射为 ``group_ids=[]``, 不产生写;
- legacy 单值 ``group_id`` 列 → 只读映射为 ``group_ids=[group_id]``;
- 第一次实际写入时, 若旧文件存在则备份一次 ``watchlist.parquet.bak``,
  然后 clean-cutover 写 canonical 4 列 schema 并删除旧列。

所有公开读写函数接受显式 ``data_dir`` (API 层传 request.app.state.repo.store.data_dir,
测试传 tmp_path); 不传时回退到 ``settings.data_dir``, 旧调用兼容。
写操作全部在进程内锁保护下 read-modify-write, 并使用原子写落盘。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from app.config import settings
from app.capabilities import Cap, CapabilitySet
from app.storage.atomic_write import atomic_write_parquet

logger = logging.getLogger(__name__)


def _get_data_provider():
    """复用 kline_sync 的 provider 工厂。"""
    from app.services.kline_sync import _get_data_provider as _factory

    return _factory()


# ── 分组常量 ────────────────────────────────────────────
_MAX_GROUP_NAME_LENGTH = 24
DEFAULT_GROUP_COLOR = "sky"
GROUP_COLORS = frozenset(
    {
        "sky",
        "blue",
        "indigo",
        "violet",
        "fuchsia",
        "rose",
        "orange",
        "amber",
        "lime",
        "emerald",
        "teal",
        "cyan",
    }
)
_GROUP_ID_RE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")

# canonical entry schema: symbol / added_at / note / group_ids(list[str])
_ENTRY_SCHEMA = {
    "symbol": pl.Utf8,
    "added_at": pl.Utf8,
    "note": pl.Utf8,
    "group_ids": pl.List(pl.Utf8),
}

# 全部 RMW 在同一进程内锁下进行 (跨 data_dir 串行, 保证同目录读改写不丢更新)
_LOCK = threading.RLock()

# 数据版本号: 每次写盘 +1 (在 _LOCK 内递增, 读取免锁)。供监控引擎等进程内
# 消费方做缓存失效判断 —— 版本没变就不必重读文件, 版本一变立即拿到新成员。
_REVISIONS: dict[str, int] = {}


def revision(data_dir: Path | str | None = None) -> int:
    """自选/分组数据版本号, 每次写操作递增 (按 data_dir 分桶)。"""
    with _LOCK:
        return _REVISIONS.get(str(_resolve(data_dir)), 0)


def _resolve(data_dir: Path | str | None) -> Path:
    return Path(data_dir) if data_dir is not None else settings.data_dir


def _path(data_dir: Path | str | None = None) -> Path:
    p = _resolve(data_dir) / "user_data" / "watchlist.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _groups_path(data_dir: Path | str | None = None) -> Path:
    p = _resolve(data_dir) / "user_data" / "watchlist_groups.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class WatchlistGroupError(ValueError):
    """分组操作入参非法 (名称/颜色/顺序等)。"""


class DuplicateGroupNameError(WatchlistGroupError):
    """分组名称与现有分组重复。"""


# ── entries 读写 ────────────────────────────────────────
def _empty_entries() -> pl.DataFrame:
    return pl.DataFrame(schema=_ENTRY_SCHEMA)


def _read_entries(data_dir: Path | str | None) -> pl.DataFrame:
    p = _path(data_dir)
    if not p.exists():
        return _empty_entries()
    df = pl.read_parquet(p)
    # 旧 schema 兼容 (只读映射, 不写回): 单值 group_id → group_ids=[gid];
    # 两列都缺 → 空列表
    if "group_ids" not in df.columns:
        old = df["group_id"].to_list() if "group_id" in df.columns else [None] * df.height
        df = df.with_columns(
            pl.Series("group_ids", [[g] if g else [] for g in old], dtype=pl.List(pl.Utf8))
        ).drop("group_id", strict=False)
    if "symbol" not in df.columns:
        df = df.with_columns(pl.lit("", dtype=pl.Utf8).alias("symbol"))
    if "added_at" not in df.columns:
        df = df.with_columns(pl.lit("", dtype=pl.Utf8).alias("added_at"))
    if "note" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias("note"))
    return df.select(list(_ENTRY_SCHEMA))


def _ensure_migration_backup(path: Path) -> None:
    """旧 schema 切换前原子生成并校验备份；失败时禁止覆盖原文件。"""
    backup = path.with_suffix(path.suffix + ".bak")
    if backup.exists():
        pl.read_parquet_schema(backup)
        return
    pending = backup.with_suffix(backup.suffix + ".tmp")
    try:
        shutil.copy2(path, pending)
        pl.read_parquet_schema(pending)
        os.replace(pending, backup)
    finally:
        pending.unlink(missing_ok=True)


def _write_entries(data_dir: Path | str | None, df: pl.DataFrame) -> None:
    path = _path(data_dir)
    # 第一次从旧 schema (无 group_ids 列) 迁移前，必须先得到可读的原子备份。
    if path.exists() and "group_ids" not in pl.read_parquet_schema(path).names():
        _ensure_migration_backup(path)
    atomic_write_parquet(df.select(list(_ENTRY_SCHEMA)), path)
    _bump_revision(data_dir)


def _bump_revision(data_dir: Path | str | None) -> None:
    key = str(_resolve(data_dir))
    _REVISIONS[key] = _REVISIONS.get(key, 0) + 1


# ── groups 读写 ────────────────────────────────────────
def _read_groups(data_dir: Path | str | None) -> list[dict]:
    p = _groups_path(data_dir)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WatchlistGroupError("自选分组配置损坏, 请检查 watchlist_groups.json") from exc
    if not isinstance(raw, list):
        raise WatchlistGroupError("自选分组配置格式不正确")
    groups = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        color = str(item.get("color", DEFAULT_GROUP_COLOR))
        groups.append(
            {
                "id": str(item["id"]),
                "name": str(item["name"]),
                "color": color if color in GROUP_COLORS else DEFAULT_GROUP_COLOR,
            }
        )
    return groups


def _atomic_write_json(path: Path, payload: list[dict]) -> None:
    """tmp + os.replace 的原子 JSON 写 (参照 storage.atomic_write 惯例)。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _write_groups(data_dir: Path | str | None, groups: list[dict]) -> None:
    p = _groups_path(data_dir)
    _atomic_write_json(p, groups)
    _bump_revision(data_dir)


def _normalize_group_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise WatchlistGroupError("分组名称不能为空")
    if len(normalized) > _MAX_GROUP_NAME_LENGTH:
        raise WatchlistGroupError(f"分组名称不能超过 {_MAX_GROUP_NAME_LENGTH} 个字符")
    return normalized


def _normalize_group_color(color: str | None) -> str:
    normalized = (color or DEFAULT_GROUP_COLOR).strip().lower()
    if normalized not in GROUP_COLORS:
        raise WatchlistGroupError("不支持的分组颜色")
    return normalized


def _validate_group_id(group_id: str | None, groups: list[dict]) -> None:
    if group_id is not None and not any(group["id"] == group_id for group in groups):
        raise WatchlistGroupError("自选分组不存在")


# ── entries 公开操作 ───────────────────────────────────
def list_symbols(data_dir: Path | str | None = None) -> list[dict]:
    with _LOCK:
        df = _read_entries(data_dir)
        return [] if df.is_empty() else df.to_dicts()


def add(
    symbol: str,
    note: str = "",
    group_id: str | None = None,
    data_dir: Path | str | None = None,
) -> list[dict]:
    rows, _ = add_batch([symbol], note=note, group_id=group_id, data_dir=data_dir)
    return rows


def add_batch(
    symbols: list[str],
    note: str = "",
    group_id: str | None = None,
    data_dir: Path | str | None = None,
) -> tuple[list[dict], int]:
    """批量添加并保持既有语义: 每个新处理的标的移动到列表最前面。

    group_id 为可选的初始分组 (如从某分组页签添加时); 重复添加的标的保留
    既有全部分组, 仅在显式传入 group_id 且尚未属于该组时并入。
    """
    with _LOCK:
        groups = _read_groups(data_dir)
        _validate_group_id(group_id, groups)
        rows = _read_entries(data_dir).to_dicts()
        added = 0
        for symbol in symbols:
            existing = next((row for row in rows if row["symbol"] == symbol), None)
            if existing is None:
                added += 1
            rows = [row for row in rows if row["symbol"] != symbol]
            gids = list((existing or {}).get("group_ids") or [])
            if group_id is not None and group_id not in gids:
                gids.append(group_id)
            rows.insert(
                0,
                {
                    "symbol": symbol,
                    "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "note": note,
                    "group_ids": gids,
                },
            )
        out = pl.DataFrame(rows, schema=_ENTRY_SCHEMA) if rows else _empty_entries()
        _write_entries(data_dir, out)
        return out.to_dicts(), added


def remove(symbol: str, data_dir: Path | str | None = None) -> list[dict]:
    with _LOCK:
        df = _read_entries(data_dir).filter(pl.col("symbol") != symbol)
        _write_entries(data_dir, df)
        return df.to_dicts()


def move_to_top(symbol: str, data_dir: Path | str | None = None) -> list[dict]:
    with _LOCK:
        df = _read_entries(data_dir)
        if df.is_empty() or symbol not in df["symbol"].to_list():
            return df.to_dicts()
        target = df.filter(pl.col("symbol") == symbol)
        rest = df.filter(pl.col("symbol") != symbol)
        out = pl.concat([target, rest], how="diagonal_relaxed")
        _write_entries(data_dir, out)
        return out.to_dicts()


def clear(data_dir: Path | str | None = None) -> int:
    """清空自选列表。返回移除的数量。"""
    with _LOCK:
        df = _read_entries(data_dir)
        count = df.height
        if count > 0:
            _write_entries(data_dir, _empty_entries())
        return count


# ── groups 公开操作 ────────────────────────────────────
def list_groups(data_dir: Path | str | None = None) -> list[dict]:
    with _LOCK:
        return _read_groups(data_dir)


def create_group(
    name: str,
    color: str | None = None,
    data_dir: Path | str | None = None,
) -> tuple[list[dict], dict]:
    with _LOCK:
        normalized = _normalize_group_name(name)
        normalized_color = _normalize_group_color(color)
        groups = _read_groups(data_dir)
        if any(group["name"].casefold() == normalized.casefold() for group in groups):
            raise DuplicateGroupNameError("分组名称已存在")
        group = {
            "id": uuid.uuid4().hex,
            "name": normalized,
            "color": normalized_color,
        }
        groups.append(group)
        _write_groups(data_dir, groups)
        return groups, group


def rename_group(
    group_id: str,
    name: str,
    color: str | None = None,
    data_dir: Path | str | None = None,
) -> list[dict]:
    with _LOCK:
        normalized = _normalize_group_name(name)
        groups = _read_groups(data_dir)
        target = next((group for group in groups if group["id"] == group_id), None)
        if target is None:
            raise KeyError(group_id)
        if any(
            group["id"] != group_id and group["name"].casefold() == normalized.casefold()
            for group in groups
        ):
            raise DuplicateGroupNameError("分组名称已存在")
        target["name"] = normalized
        if color is not None:
            target["color"] = _normalize_group_color(color)
        _write_groups(data_dir, groups)
        return groups


def reorder_groups(ordered_ids: list[str], data_dir: Path | str | None = None) -> list[dict]:
    """按给定 id 顺序重排分组 (json 数组顺序即定义顺序)。"""
    with _LOCK:
        groups = _read_groups(data_dir)
        by_id = {group["id"]: group for group in groups}
        if len(ordered_ids) != len(groups) or set(ordered_ids) != set(by_id):
            raise WatchlistGroupError("分组顺序与现有分组不一致")
        reordered = [by_id[group_id] for group_id in ordered_ids]
        _write_groups(data_dir, reordered)
        return reordered


def delete_group(
    group_id: str, data_dir: Path | str | None = None
) -> tuple[list[dict], list[dict]]:
    """删除分组定义, 原分组内的自选保留并转为未分组 (仅摘掉该组标签)。"""
    with _LOCK:
        groups = _read_groups(data_dir)
        if not any(group["id"] == group_id for group in groups):
            raise KeyError(group_id)
        df = _strip_group(_read_entries(data_dir), group_id)
        remaining = [group for group in groups if group["id"] != group_id]
        _write_entries(data_dir, df)
        _write_groups(data_dir, remaining)
        return remaining, df.to_dicts()


def clear_group(group_id: str, data_dir: Path | str | None = None) -> list[dict]:
    """清空分组成员: 把该分组标签从所有条目摘掉 (变未分组), 保留分组定义。"""
    with _LOCK:
        groups = _read_groups(data_dir)
        if not any(group["id"] == group_id for group in groups):
            raise KeyError(group_id)
        df = _strip_group(_read_entries(data_dir), group_id)
        _write_entries(data_dir, df)
        return df.to_dicts()


def _strip_group(df: pl.DataFrame, group_id: str) -> pl.DataFrame:
    """从所有条目的 group_ids 中摘掉指定分组 (删除分组/清空分组共用)。"""
    rows = df.to_dicts()
    for row in rows:
        gids = row.get("group_ids") or []
        if group_id in gids:
            row["group_ids"] = [g for g in gids if g != group_id]
    return pl.DataFrame(rows, schema=_ENTRY_SCHEMA) if rows else _empty_entries()


# ── membership (M:N) ───────────────────────────────────
def set_group(
    symbol: str,
    group_id: str | None,
    data_dir: Path | str | None = None,
) -> list[dict]:
    """互斥设定: 该标的只保留这一个分组 (group_id=None 即全部移出, 变未分组)。

    多组模型的日常操作走 add_to_group / remove_from_group; 本函数服务于
    「仅保留此组」的显式场景。
    """
    with _LOCK:
        groups = _read_groups(data_dir)
        _validate_group_id(group_id, groups)
        rows = _read_entries(data_dir).to_dicts()
        if not any(row["symbol"] == symbol for row in rows):
            raise KeyError(symbol)
        for row in rows:
            if row["symbol"] == symbol:
                row["group_ids"] = [group_id] if group_id is not None else []
        out = pl.DataFrame(rows, schema=_ENTRY_SCHEMA)
        _write_entries(data_dir, out)
        return out.to_dicts()


def add_to_group(
    symbol: str,
    group_id: str,
    data_dir: Path | str | None = None,
) -> list[dict]:
    """把标的加入一个分组 (多组成员关系: 不影响已属于的其他分组; 幂等)。"""
    with _LOCK:
        groups = _read_groups(data_dir)
        _validate_group_id(group_id, groups)
        rows = _read_entries(data_dir).to_dicts()
        if not any(row["symbol"] == symbol for row in rows):
            raise KeyError(symbol)
        for row in rows:
            if row["symbol"] == symbol:
                gids = row["group_ids"] or []
                if group_id not in gids:
                    gids.append(group_id)
                    row["group_ids"] = gids
        out = pl.DataFrame(rows, schema=_ENTRY_SCHEMA)
        _write_entries(data_dir, out)
        return out.to_dicts()


def remove_from_group(
    symbol: str,
    group_id: str,
    data_dir: Path | str | None = None,
) -> list[dict]:
    """把标的移出一个分组 (仅摘本组标签; 标的仍在自选; 幂等)。"""
    with _LOCK:
        groups = _read_groups(data_dir)
        _validate_group_id(group_id, groups)
        rows = _read_entries(data_dir).to_dicts()
        if not any(row["symbol"] == symbol for row in rows):
            raise KeyError(symbol)
        for row in rows:
            if row["symbol"] == symbol:
                row["group_ids"] = [g for g in (row["group_ids"] or []) if g != group_id]
        out = pl.DataFrame(rows, schema=_ENTRY_SCHEMA)
        _write_entries(data_dir, out)
        return out.to_dicts()


def group_members(group_id: str, data_dir: Path | str | None = None) -> list[str]:
    """返回分组当前成员 symbol 列表。分组不存在/为空/读取失败 → 空列表 (fail-closed)。"""
    try:
        with _LOCK:
            rows = _read_entries(data_dir).to_dicts()
    except Exception as e:  # noqa: BLE001
        logger.warning("read watchlist entries for group %s failed: %s", group_id, e)
        return []
    return [str(row["symbol"]) for row in rows if group_id in (row.get("group_ids") or [])]


def fetch_quotes(symbols: list[str], capset: CapabilitySet, timeout_s: float = 8.0) -> list[dict]:
    """拉取实时行情。

    通过 data_providers 抽象层取数,支持 provider 切换。
    - realtime provider: 走 provider.get_realtime, 有实时数据
    - fquant/fquant_local provider: 走本地 realtime fallback，不可用时优雅降级为空
    timeout_s: 单批次请求超时(秒)，防止 API 卡死阻塞整个请求。
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    if not symbols:
        return []

    provider = _get_data_provider()
    quotes: list[dict] = []

    # 走 batch
    batch_size = 5
    if capset.has(Cap.QUOTE_BATCH):
        lim = capset.limits(Cap.QUOTE_BATCH)
        batch_size = lim.batch if lim and lim.batch else 50
    elif capset.has(Cap.QUOTE_BY_SYMBOL):
        lim = capset.limits(Cap.QUOTE_BY_SYMBOL)
        batch_size = lim.batch if lim and lim.batch else 5
    else:
        # 无任何实时行情能力(none/free 档走 free-api 服务器,不提供实时行情)
        # 提前返回空,避免发起注定失败的请求
        return []

    # provider 不支持 realtime 时,直接降级返回空,不调 SDK
    if not getattr(provider.capabilities, "realtime", False):
        logger.info(
            "watchlist: 当前 provider %s 不支持 realtime, 降级返回空",
            provider.name,
        )
        return []

    chunks = [symbols[i : i + batch_size] for i in range(0, len(symbols), batch_size)]

    # 用线程池为每个批次加超时保护
    pool = ThreadPoolExecutor(max_workers=1)
    for chunk in chunks:
        try:
            future = pool.submit(provider.get_realtime, symbols=chunk)
            raw = future.result(timeout=timeout_s)
            if raw is None or len(raw) == 0:
                continue
            df = pl.from_pandas(raw) if hasattr(raw, "iteritems") else raw
            quotes.extend(df.to_dicts())
        except FuturesTimeout:
            logger.warning("quote fetch timeout (%.1fs) for %d symbols", timeout_s, len(chunk))
            break  # 超时后不再尝试后续批次
        except Exception as e:  # noqa: BLE001
            logger.warning("quote fetch failed for %d symbols: %s", len(chunk), e)
    pool.shutdown(wait=False)

    return quotes
