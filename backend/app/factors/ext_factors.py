"""扩展表字段 → 因子/信号接入 (单一原语, 两个消费方)。

扩展数据 (data/ext_data/{config_id}) 的数值字段在 enriched 帧组装时 join 到帧上,
并以 kind="base" (空依赖 = 已物化列自身) 注册进因子注册表:

- 自定义信号: custom_signals.allowed_fields() 并入注册表因子, 扩展列出现在
  信号条件字段下拉中 (all_factors → ensure_synced 惰性同步);
- 因子/评分/检验: scoring_value_expr 对帧上已有列直接 pl.col 引用,
  注册表条目让扩展字段同时出现在因子库列表与 AI 提示词中。

口径与边界 (金融契约, 见 CONTRIBUTING §3/§5.3):
- timeseries 模式: 按 (symbol, date) 分区日期精确对齐, 历史帧无未来函数;
- snapshot 模式: 代表"最新值", 仅在单日帧 (compute_enriched_today 盘中/当日)
  注入; 多日历史帧跳过, 否则回测/历史回看会引入未来数据;
- 数值字段 (int/float, 统一 Float64): 因子 + 信号双通道 (注册表 base 条目);
- string 字段: 仅信号条件通道 (contains/==/!= 字符串运算符, 概念/行业归属
  筛选), 不注册为因子 —— 因子 IC/排序是数值口径; bool 不参与。

缓存与失效 (CONTRIBUTING §6.1):
- 配置清单复用 ExtConfigStore.load_all 的目录签名缓存;
- 已加载的扩展帧按 (config 目录/分区签名) 缓存, 数据/配置变更后由
  invalidate_ext_caches 清除 (写入端 write_ext_parquet / upsert / delete 自动调用),
  同时清策略结果缓存 —— 策略历史窗口与 enriched 内存缓存里的帧含旧扩展列。
"""
from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path
from typing import Any

import polars as pl

from app.factors.registry import FactorSpec, get_factor, register_factor, unregister_factor

logger = logging.getLogger(__name__)

EXT_PREFIX = "ext_"
_NUMERIC_DTYPES = frozenset({"int", "float"})
# 信号通道支持的 dtype: 数值 (Float64) + 字符串 (Utf8, contains/==/!=)
_SIGNAL_DTYPES = _NUMERIC_DTYPES | {"string"}

# 帧缓存: (data_dir, config_id, mode) -> (目录/分区签名, DataFrame)
_frame_cache: dict[tuple[str, str, str], tuple[tuple, pl.DataFrame]] = {}
# 注册同步状态: (data_dir, 配置签名); None/失配 → 下次调用重新同步。
# 已注册集合以注册表为权威 (ext_ 前缀条目), 不单独记账 —— 失效入口清空
# 状态后, 重新同步仍能从注册表注销已移除的扩展因子。
_sync_state: tuple | None = None


def ext_column_name(config_id: str, field_name: str) -> str:
    """扩展字段在帧/信号中的列名: ext_{config_id}_{field}。

    保留中日韩文字 (\\w 含 unicode 字母) —— 预设表的字段名多为中文
    (所属概念/股票简称), 全部折叠为 ASCII 会互相碰撞。非单词字符转下划线。
    """
    sanitized = re.sub(r"[^\w]+", "_", field_name, flags=re.UNICODE).strip("_") or "f"
    return f"{EXT_PREFIX}{config_id}_{sanitized}"


def _resolve_dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    from app.config import settings

    return Path(settings.data_dir)


def _load_configs(data_dir: Path):
    from app.services.ext_data import ExtConfigStore

    return ExtConfigStore(data_dir).load_all()


def _numeric_fields(config) -> list:
    return [f for f in config.fields if f.dtype in _NUMERIC_DTYPES]


def numeric_timeseries_ext_columns(data_dir: Path | None = None) -> frozenset[str]:
    """时序模式数值扩展字段的列名集合 — 回测矩阵可注入的白名单。

    快照模式 (mode=snapshot) 刻意排除: 快照是"最新值", 回看历史注入即未来函数,
    帧路径的 include_snapshot=False 同理只放行时序。
    """
    root = _resolve_dir(data_dir)
    return frozenset(
        ext_column_name(cfg.id, f.name)
        for cfg in _load_configs(root)
        if cfg.mode == "timeseries"
        for f in _numeric_fields(cfg)
    )


def _signal_fields(config) -> list:
    """帧 join / 信号条件可用的字段 (数值 + 字符串)。"""
    return [f for f in config.fields if f.dtype in _SIGNAL_DTYPES]


def ext_string_fields(data_dir: Path | None = None) -> frozenset[str]:
    """string 扩展字段的列名集合 (仅供信号条件, 不注册为因子)。"""
    return frozenset(e["key"] for e in ext_string_field_entries(data_dir))


def ext_string_field_entries(data_dir: Path | None = None) -> list[dict[str, str]]:
    """string 扩展字段条目 [{key, label}], 供 /options 与 AI 提示词展示。"""
    root = _resolve_dir(data_dir)
    return [
        {"key": ext_column_name(cfg.id, f.name), "label": f"{cfg.label}·{f.label or f.name}"[:40]}
        for cfg in _load_configs(root)
        for f in cfg.fields
        if f.dtype == "string"
    ]


def ext_factor_specs(data_dir: Path | None = None) -> list[FactorSpec]:
    """扩展表数值字段的 base 因子条目 (列自身即值, 无依赖)。

    id 含非 ASCII (中文字段名) 的字段跳过注册: DSL 公式标识符是
    ASCII-only, 注册一个公式里写不出来的因子只会误导; 该列仍参与
    帧 join, 信号条件 (数值比较) 照常可用。
    """
    root = _resolve_dir(data_dir)
    specs: list[FactorSpec] = []
    for cfg in _load_configs(root):
        for f in _numeric_fields(cfg):
            fid = ext_column_name(cfg.id, f.name)
            if not fid.isascii():
                continue
            specs.append(FactorSpec(
                id=fid,
                label=f"{cfg.label}·{f.label}"[:32],
                group="扩展数据",
                formula_text=(
                    f"扩展表「{cfg.label}」字段 {f.name} "
                    f"({'时序·按交易日对齐' if cfg.mode == 'timeseries' else '最新快照·仅当日帧'})"
                ),
                kind="base",
                warmup_bars=1,
                scale_free=False,
                tags=("ext", cfg.id),
            ))
    return specs


def ext_factor_ids(data_dir: Path | None = None) -> frozenset[str]:
    """当前扩展因子 id 集合 (供补算入口判断是否需要注入扩展列)。"""
    return frozenset(s.id for s in ext_factor_specs(data_dir))


def ensure_synced(data_dir: Path | None = None) -> None:
    """把扩展因子同步进注册表 (幂等, 按配置目录签名跳过)。

    以注册表中已存在的 ext_ 前缀条目为权威做增删 —— 不触碰内置目录与
    用户自定义因子 (uf_/cf_)。重复注册采用"先注销再注册"模式
    (与 api/factors.py 状态迁移一致), 避免版本未提升时的 fail-closed 拒绝。
    """
    global _sync_state
    root = _resolve_dir(data_dir)
    from app.services.ext_data import _ext_config_dir_signature

    ext_base = root / "ext_data"
    # 目录不存在 = 明确的"无配置" (空签名, 继续同步以清理残留注册);
    # 目录存在但扫描失败才跳过 (fail-open, 不清空已注册条目)。
    if not ext_base.exists():
        sig: tuple | None = ()
    else:
        sig = _ext_config_dir_signature(ext_base)
    if sig is None:
        return
    key = (str(root), sig)
    if _sync_state == key:
        return
    desired = ext_factor_specs(root)
    desired_ids = {s.id for s in desired}
    from app.factors.registry import _REGISTRY

    for fid in [f for f in list(_REGISTRY) if f.startswith(EXT_PREFIX) and f not in desired_ids]:
        try:
            unregister_factor(fid)
        except ValueError:
            logger.warning("扩展因子注销失败: %s", fid)
    for spec in desired:
        if get_factor(spec.id) is not None:
            with contextlib.suppress(ValueError):
                unregister_factor(spec.id)
        register_factor(spec)
    _sync_state = key


def _timeseries_signature(ts_dir: Path) -> tuple | None:
    """时序分区签名: (分区目录名, part.parquet mtime_ns, size)。"""
    try:
        sig = []
        for d in sorted(ts_dir.glob("date=*")):
            part = d / "part.parquet"
            if d.is_dir() and part.exists():
                st = part.stat()
                sig.append((d.name, st.st_mtime_ns, st.st_size))
        return tuple(sig)
    except OSError:
        return None


def _select_fields(df: pl.DataFrame, config, fields: list, *, with_date: str | None) -> pl.DataFrame:
    """选列 + 统一 dtype: int/float → Float64 (数值阈值), string → Utf8 (contains)。"""
    exprs = [pl.col("symbol").cast(pl.Utf8)]
    for f in fields:
        name = ext_column_name(config.id, f.name)
        if f.name not in df.columns:
            continue  # 分区 schema 漂移: 缺列以 null 补 (diagonal concat)
        dtype = pl.Float64 if f.dtype in _NUMERIC_DTYPES else pl.Utf8
        exprs.append(pl.col(f.name).cast(dtype).alias(name))
    if len(exprs) == 1:
        return pl.DataFrame()
    out = df.select(exprs)
    if with_date is not None:
        out = out.with_columns(pl.lit(with_date).alias("_ext_date"))
    return out


def _timeseries_frame(root: Path, config, fields: list) -> pl.DataFrame:
    """全量时序扩展帧 (symbol, _ext_date, ext 列); 按分区签名缓存。

    缓存不过滤日期范围: 调用方用帧自身日期范围在 join 后自然裁剪,
    避免按日期范围缓存导致的键膨胀。
    """
    ts_dir = root / "ext_data" / config.id / "timeseries"
    sig = _timeseries_signature(ts_dir)
    if sig is not None and not sig:
        return pl.DataFrame()
    key = (str(root), config.id, "timeseries")
    if sig is not None:
        cached = _frame_cache.get(key)
        if cached is not None and cached[0] == sig:
            return cached[1]
    parts: list[pl.DataFrame] = []
    if sig is not None:
        for d in sorted(ts_dir.glob("date=*")):
            part = d / "part.parquet"
            if not (d.is_dir() and part.exists()):
                continue
            try:
                raw = pl.read_parquet(part)
            except Exception as e:
                logger.warning("扩展表 %s 分区 %s 读取失败, 跳过: %s", config.id, d.name, e)
                continue
            frag = _select_fields(raw, config, fields, with_date=d.name[5:])
            if not frag.is_empty():
                parts.append(frag)
    frame = (
        pl.concat(parts, how="diagonal").unique(subset=["symbol", "_ext_date"], keep="last")
        if parts else pl.DataFrame()
    )
    if sig is not None:
        _frame_cache[key] = (sig, frame)
    return frame


def _snapshot_frame(root: Path, config, fields: list) -> pl.DataFrame:
    """快照扩展帧 (symbol, ext 列); 按 part.parquet (mtime, size) 签名缓存。"""
    path = root / "ext_data" / config.id / "part.parquet"
    try:
        sig = None
        if path.exists():
            st = path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        if sig is None:
            return pl.DataFrame()
        key = (str(root), config.id, "snapshot")
        cached = _frame_cache.get(key)
        if cached is not None and cached[0] == sig:
            return cached[1]
        frame = _select_fields(pl.read_parquet(path), config, fields, with_date=None)
        if not frame.is_empty():
            frame = frame.unique(subset=["symbol"], keep="last")
        _frame_cache[key] = (sig, frame)
        return frame
    except Exception as e:
        logger.warning("扩展表 %s 快照读取失败, 跳过: %s", config.id, e)
        return pl.DataFrame()


def attach_ext_columns(
    df: pl.DataFrame,
    *,
    include_snapshot: bool,
    data_dir: Path | None = None,
) -> pl.DataFrame:
    """把扩展表信号列 (数值 + 字符串) join 到 enriched 帧上 (无配置/无匹配时原样返回)。

    include_snapshot 仅应由单日帧 (当日/盘中) 路径传 True; 多日历史帧
    传 False 以规避快照"最新值"造成的未来函数。单个配置失败只跳过该配置。
    """
    if df.is_empty() or "symbol" not in df.columns:
        return df
    root = _resolve_dir(data_dir)
    configs = _load_configs(root)
    if not configs:
        return df

    if "_ext_date" in df.columns:  # pragma: no cover - 防御内部临时列名被占用
        return df
    has_date = "date" in df.columns
    tmp_date = False
    try:
        for cfg in configs:
            fields = _signal_fields(cfg)
            if not fields:
                continue
            try:
                if cfg.mode == "timeseries":
                    if not has_date:
                        continue  # 无日期列无法 PIT 对齐, 跳过 (ETF/指数单行帧等)
                    ext = _timeseries_frame(root, cfg, fields)
                    if ext.is_empty():
                        continue
                    if not tmp_date:
                        df = df.with_columns(pl.col("date").cast(pl.Utf8).alias("_ext_date"))
                        tmp_date = True
                    new_cols = [c for c in ext.columns if c not in df.columns and c != "_ext_date"]
                    if not new_cols:
                        continue
                    df = df.join(
                        ext.select(["symbol", "_ext_date", *new_cols]),
                        on=["symbol", "_ext_date"],
                        how="left",
                    )
                elif include_snapshot:
                    snap = _snapshot_frame(root, cfg, fields)
                    if snap.is_empty():
                        continue
                    new_cols = [c for c in snap.columns if c not in df.columns]
                    if not new_cols:
                        continue
                    df = df.join(snap.select(["symbol", *new_cols]), on="symbol", how="left")
            except Exception as e:
                logger.warning("扩展表 %s 列注入失败, 跳过该表: %s", cfg.id, e)
    finally:
        if tmp_date:
            df = df.drop("_ext_date")
    return df


def attach_matrix_ext_fields(market: Any, data_dir: Path | None, names: Any) -> Any:
    """把时序扩展数值列作为 matrix fields 附加到 (frozen) MarketDataMatrix 副本。

    回测矩阵只读 enriched parquet 的 14 个存储列 (扩展列按设计不落盘, 见
    pipeline._select_storage_cols); 扩展数据在这里按矩阵自身的 (日期×标的)
    骨架现场 join — 与策略页帧路径同一台 attach_ext_columns (PIT 安全:
    include_snapshot=False, 只进时序模式)。left join 保持骨架行序, 直接
    reshape 为 (T, A); 缺失 (标的, 日期) 为 NaN, 与叠加条件掩码"NaN 不命中"
    及帧路径 fill_null(False) 同语义。财务因子的 attach_matrix_fundamental_fields
    是同构先例。
    """
    import dataclasses
    from types import MappingProxyType

    import numpy as np

    wanted = sorted({str(n) for n in names})
    if not wanted:
        return market
    labels = list(market.timestamp_labels)
    symbols = list(market.symbols)
    n_t, n_a = len(labels), len(symbols)
    if n_t == 0 or n_a == 0:
        return market
    skeleton = pl.DataFrame({
        "date": np.repeat(np.array(labels, dtype=object), n_a),
        "symbol": np.tile(np.array(symbols, dtype=object), n_t),
    })
    enriched = attach_ext_columns(skeleton, include_snapshot=False, data_dir=data_dir)
    missing = [c for c in wanted if c not in enriched.columns]
    if missing:
        raise ValueError(
            f"扩展字段数据不可用: {missing} — 检查扩展数据是否已拉取、字段是否仍存在"
        )
    extra: dict[str, np.ndarray] = {}
    for name in wanted:
        arr = (
            enriched[name]
            .cast(pl.Float64)
            .fill_null(float("nan"))
            .to_numpy()
            .astype(np.float32)
        )
        arr = arr.reshape(n_t, n_a)
        arr.flags.writeable = False
        extra[name] = arr
    merged = {**dict(market.fields), **extra}
    return dataclasses.replace(market, fields=MappingProxyType(merged))


def invalidate_ext_caches(data_dir: Path | None = None, *, keep_strategy_cache: bool = False) -> None:
    """扩展数据/配置变更后的失效入口 (写入端自动调用)。

    清扩展帧缓存与注册同步状态 (下次读取重新加载), 并清策略结果缓存 ——
    策略历史窗口磁盘缓存里已含旧扩展列。repo 内存 enriched 缓存由
    API 层 (repo.clear_cache) 补充清理。

    keep_strategy_cache=True: 例行数据刷新 (定时拉取) 只失效帧缓存 —— 下次
    策略运行自然读到新值, 但不销毁已算好的结果。周期性清空会让策略页在两次
    重算之间整页空白 (小服务器上全量重算需分钟级), 例行刷新的取舍是保留旧
    结果 (页面秒加载) 而非黑屏; 手动上传/配置变更仍走全清。
    """
    global _sync_state
    root_key = str(_resolve_dir(data_dir))
    for key in [k for k in _frame_cache if k[0] == root_key]:
        _frame_cache.pop(key, None)
    _sync_state = None
    if keep_strategy_cache:
        return
    from app.config import settings as _settings
    from app.services import strategy_cache

    try:
        strategy_cache.clear_cache(Path(data_dir) if data_dir else Path(_settings.data_dir))
    except Exception as e:
        logger.warning("扩展数据变更后策略缓存清理失败: %s", e)
