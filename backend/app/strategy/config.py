"""策略配置持久化 — 读写用户覆盖值。

职责: 将每个策略的用户定制设置（基础参数、策略参数、评分、买卖信号）持久化到 JSON。
不知道: 引擎、AI、前端、回测。
存储: data/user_data/strategy_overrides/{strategy_id}.json
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# 进程内缓存: 监控引擎每轮对每条策略规则调用 load_override, 每次读盘+parse 纯重复;
# override 仅在用户编辑时变化, 以 (mtime_ns, size) 签名判断是否重读。
# 键为 override 文件路径 (进程内可能有多个 data_dir, 如测试)。
_override_cache: dict[str, dict] = {}
_override_cache_sig: dict[str, tuple[int, int]] = {}


def _invalidate_override_cache(path: Path) -> None:
    key = str(path)
    _override_cache.pop(key, None)
    _override_cache_sig.pop(key, None)


def _overrides_dir(data_dir: Path) -> Path:
    d = data_dir / "user_data" / "strategy_overrides"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(data_dir: Path, strategy_id: str, *, ensure_dir: bool = True) -> Path:
    # ensure_dir=False 供热路径读取: mkdir 系统调用在 Windows 上 ~0.07ms,
    # 读缓存命中时跳过它 (目录由写路径保证存在)。
    if ensure_dir:
        d = _overrides_dir(data_dir)
    else:
        d = data_dir / "user_data" / "strategy_overrides"
    return d / f"{strategy_id}.json"


def normalize_override(data: dict) -> dict:
    """返回单项读取使用的有效配置，不改写输入或保存文件。"""
    data = copy.deepcopy(data)
    bf = data.get("basic_filter")
    if isinstance(bf, dict):
        cleaned = {k: v for k, v in bf.items() if v is not None}
        if cleaned:
            data["basic_filter"] = cleaned
        else:
            del data["basic_filter"]
    return data


def load_override(data_dir: Path, strategy_id: str) -> dict:
    """读取策略的用户覆盖配置，不存在返回空 dict (带 mtime 签名缓存, 返回深拷贝)"""
    p = _path(data_dir, strategy_id, ensure_dir=False)
    key = str(p)
    try:
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        _invalidate_override_cache(p)
        return {}
    cached = _override_cache.get(key)
    if cached is not None and sig == _override_cache_sig.get(key):
        return copy.deepcopy(cached)
    try:
        data = normalize_override(json.loads(p.read_text(encoding="utf-8")))
        _override_cache[key] = data
        _override_cache_sig[key] = sig
        return copy.deepcopy(data)
    except Exception as e:
        logger.warning("load override %s failed: %s", strategy_id, e)
        return {}


def save_override(data_dir: Path, strategy_id: str, overrides: dict) -> None:
    """保存策略的用户覆盖配置（全量覆盖写）"""
    p = _path(data_dir, strategy_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(overrides, ensure_ascii=False, indent=2))
    _invalidate_override_cache(p)


def delete_override(data_dir: Path, strategy_id: str) -> None:
    """删除策略的用户覆盖配置（重置为默认值）"""
    p = _path(data_dir, strategy_id)
    _invalidate_override_cache(p)
    if p.exists():
        p.unlink()


def list_overrides(data_dir: Path) -> dict[str, dict]:
    """返回所有策略的覆盖配置 {strategy_id: overrides}"""
    d = _overrides_dir(data_dir)
    result: dict[str, dict] = {}
    for f in d.glob("*.json"):
        try:
            sid = f.stem
            result[sid] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return result
