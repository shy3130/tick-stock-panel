"""alpha-factory 本地 parquet 数据源 (只读, 无网络)。

数据真值来自 alpha-factory 仓库落盘的本地 parquet:

- ETF 日K: ``ETF数据更新至2026.7.10/etf_daily.parquet`` (与项目 ashare_etf_data.NEW_2026 口径一致)
- ETF 维表: ``ETF数据更新至2026.7.10/etf_basic_data.parquet``
- A 股样本日K: ``baiduyun/stock_daily.parquet``

内部数据契约 (见 docs/plugin-development.md):
- daily 返回**不复权原始价**, 复权由主程序用 adj_factor 投影
- volume 单位为**手**, amount 单位为**元** (源数据 amount 为千元, 此处 ×1000 显式换算)
- change_pct 为小数制 (源数据 pct_chg 为百分数, 此处 ÷100)

首次读取会把源数据规整成 sidecar 缓存 (DATA_DIR/cache/af_local/*.parquet) 并做单位自检;
源文件签名 (mtime+size) 变化时自动重建。单位不符直接抛错, 不静默返回错数。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities, ProviderRequestError

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path("/Volumes/data/alpha-factory/raw")

# 源数据量额口径: amount 为千元, vol 为手。
# 自检依据: amount(元) / volume(手) / close ≈ 100 (1 手 = 100 股)
_AMOUNT_TO_YUAN = 1000.0
_RATIO_EXPECTED = 100.0
_RATIO_TOLERANCE = (50.0, 200.0)

_SOURCE_COLUMNS = [
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "vol",
    "amount",
    "adj_factor",
]


def _raw_root() -> Path:
    return Path(os.environ.get("AF_LOCAL_RAW_ROOT", str(DEFAULT_ROOT)))


def _etf_paths() -> tuple[Path, Path]:
    d = Path(os.environ.get("AF_LOCAL_ETF_DIR", str(_raw_root() / "ETF数据更新至2026.7.10")))
    return d / "etf_daily.parquet", d / "etf_basic_data.parquet"


def _stock_path() -> Path:
    return Path(os.environ.get("AF_LOCAL_STOCK_PARQUET", str(_raw_root() / "baiduyun" / "stock_daily.parquet")))


def _etf_overlay_path() -> Path:
    """ETF 增量补数表（alpha-factory 每日写 overlay，快照保持只读基线）。

    与 alpha_factory/data/ashare_etf_data.py 的读时口径一致：
    只把「严格晚于快照末日」的行追加到快照之上，永不覆盖既有历史。
    """
    env = os.environ.get("AF_LOCAL_ETF_OVERLAY")
    if env:
        return Path(env)
    return _raw_root().parent / "output" / "etf_daily_overlay" / "overlay.parquet"


def _stock_overlay_path() -> Path:
    """A 股增量补数表（结构与 ETF overlay 同源，只保留价量+复权因子）。

    ``stock_daily.parquet`` 的每日增量先落 alpha-factory 的 cache，再人工提升进
    raw；没人提升时快照就永远差一天。这里读 output 下的 overlay 作为增量层。
    """
    env = os.environ.get("AF_LOCAL_STOCK_OVERLAY")
    if env:
        return Path(env)
    return _raw_root().parent / "output" / "ashare_daily_overlay" / "overlay.parquet"


def availability() -> tuple[bool, str]:
    """清单 check: 只做本地就绪检查, 不发起网络请求。"""
    daily, master = _etf_paths()
    if not daily.exists():
        return False, f"缺少 ETF 日K: {daily}"
    if not master.exists():
        return False, f"缺少 ETF 维表: {master}"
    return True, "ok"


def _read_raw(path: Path, columns: list[str]) -> pl.DataFrame:
    """读源 parquet; pandas 写入时把键列放在 index 上, 需 reset_index 兜底。"""
    if not path.exists():
        raise ProviderRequestError(f"数据文件不存在: {path}")
    try:
        return pl.read_parquet(path, columns=columns)
    except Exception:  # noqa: BLE001 — 列不在 arrow schema 里时走 pandas 兜底
        logger.info("polars 直读 %s 失败, 回退 pandas (键列可能在 index 上)", path.name)
    import pandas as pd

    try:
        pdf = pd.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        raise ProviderRequestError(f"读取 {path.name} 失败: {e}") from e
    pdf = pdf.reset_index()
    missing = [c for c in columns if c not in pdf.columns]
    if missing:
        raise ProviderRequestError(f"{path.name} 缺少必需列: {missing}")
    return pl.from_pandas(pdf[columns])


def _cache_dir() -> Path:
    from app.config import settings  # 本地导入: 插件加载期不依赖核心私有属性

    return Path(settings.data_dir) / "cache" / "af_local"


def _source_signature(path: Path, overlay: Path | None = None) -> dict:
    st = path.stat()
    sig = {"path": str(path), "mtime": int(st.st_mtime), "size": int(st.st_size)}
    if overlay is not None and overlay.exists():
        ost = overlay.stat()
        sig["overlay"] = {"path": str(overlay), "mtime": int(ost.st_mtime), "size": int(ost.st_size)}
    return sig


def _assert_units(frame: pl.DataFrame, label: str) -> None:
    """量额单位自检: amount/volume/close 应≈100(元/手/元)。"""
    sample = frame.filter(
        (pl.col("volume") > 0) & (pl.col("close") > 0) & pl.col("amount").is_not_null()
    ).head(2000)
    if sample.is_empty():
        raise ProviderRequestError(f"{label}: 无有效量额样本, 无法校验单位口径")
    ratio = (sample["amount"] / sample["volume"] / sample["close"]).median()
    lo, hi = _RATIO_TOLERANCE
    if ratio is None or not (lo <= float(ratio) <= hi):
        raise ProviderRequestError(
            f"{label}: 量额单位自检失败 amount/volume/close={ratio} (期望≈{_RATIO_EXPECTED})"
        )


def _read_raw_with_overlay(source: Path, overlay: Path | None) -> pl.DataFrame:
    """读快照, 并把 overlay 里「严格晚于快照末日」的行追加进来。

    快照是只读基线(红线), overlay 是 alpha-factory 每日增量写的补数表。
    不合并的话, 面板只能看到快照末日(实测停在 11-11), 增量永远进不来。
    只做单向追加, 不覆盖快照既有历史; overlay 缺件或损坏时回退纯快照。
    """
    raw = _read_raw(source, _SOURCE_COLUMNS)
    if overlay is None or not overlay.exists():
        return raw
    try:
        ov = _read_raw(overlay, _SOURCE_COLUMNS)
    except Exception as e:  # noqa: BLE001 — overlay 损坏不该拖垮主数据
        logger.warning("af_local overlay 读取失败, 回退纯快照: %s (%s)", overlay, e)
        return raw
    raw = raw.with_columns(pl.col("trade_date").cast(pl.Date, strict=False))
    ov = ov.with_columns(pl.col("trade_date").cast(pl.Date, strict=False))
    snap_max = raw["trade_date"].max()
    if snap_max is None:
        return raw
    ov = ov.filter(pl.col("trade_date") > snap_max)
    if ov.is_empty():
        return raw
    logger.info("af_local 叠加 overlay: %d 行 (快照末日 %s 之后)", ov.height, snap_max)
    return pl.concat([raw, ov], how="vertical_relaxed")


def _build_cache(source: Path, asset_type: str, cache_path: Path, overlay: Path | None = None) -> None:
    """把源 parquet 规整成面板内部口径的 sidecar 缓存。"""
    raw = _read_raw_with_overlay(source, overlay)
    frame = raw.rename(
        {
            "ts_code": "symbol",
            "trade_date": "date",
            "vol": "volume",
            "adj_factor": "ex_factor",
        }
    ).with_columns(
        pl.col("date").cast(pl.Date, strict=False),
        pl.col("amount").cast(pl.Float64, strict=False) * _AMOUNT_TO_YUAN,
        (pl.col("pct_chg").cast(pl.Float64, strict=False) / 100.0).alias("change_pct"),
        pl.col("symbol").cast(pl.Utf8, strict=False),
        pl.lit(asset_type).alias("asset_type"),
    )
    frame = frame.select(
        [
            "symbol",
            "date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change_pct",
            "volume",
            "amount",
            "ex_factor",
            "asset_type",
        ]
    ).drop_nulls(subset=["symbol", "date", "close"]).sort(["symbol", "date"])
    _assert_units(frame, source.name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp.parquet")
    frame.write_parquet(tmp)
    tmp.replace(cache_path)
    cache_path.with_suffix(".meta.json").write_text(
        json.dumps(
            {"signature": _source_signature(source, overlay), "rows": frame.height},
            ensure_ascii=False,
        )
    )
    logger.info("af_local 缓存重建: %s (%d 行)", cache_path.name, frame.height)


def _ensure_cache(source: Path, asset_type: str) -> Path:
    cache_path = _cache_dir() / f"{asset_type}_daily.parquet"
    meta_path = cache_path.with_suffix(".meta.json")
    if asset_type == "etf":
        overlay = _etf_overlay_path()
    elif asset_type == "stock":
        overlay = _stock_overlay_path()
    else:
        overlay = None
    signature = _source_signature(source, overlay)
    if cache_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            meta = {}
        if meta.get("signature") == signature:
            return cache_path
        logger.info("af_local 源签名变化, 重建缓存: %s", source.name)
    _build_cache(source, asset_type, cache_path, overlay=overlay)
    return cache_path


def _daily_source(asset_type: str) -> Path:
    etf_daily, _ = _etf_paths()
    if asset_type == "etf":
        return etf_daily
    if asset_type == "stock":
        return _stock_path()
    raise ProviderRequestError(f"af_local 不支持 asset_type={asset_type!r}")


_EMPTY_DAILY_SCHEMA = {
    "symbol": pl.Utf8,
    "asset_type": pl.Utf8,
    "source": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "pre_close": pl.Float64,
    "change_pct": pl.Float64,
}


def _empty_daily() -> pl.DataFrame:
    return pl.DataFrame(schema=_EMPTY_DAILY_SCHEMA)


def _as_date(value: datetime | date | None) -> date | None:
    return value.date() if isinstance(value, datetime) else value


_ETF_SYMBOL_CACHE: list[set[str]] = []


def _etf_symbol_set() -> set[str]:
    """ETF 维表代码集合 (缓存一次); 用于面板批量调用未显式给 asset_type 时判定资产类型。"""
    if not _ETF_SYMBOL_CACHE:
        _, master_path = _etf_paths()
        if not master_path.exists():
            _ETF_SYMBOL_CACHE.append(set())
        else:
            master = _read_raw(master_path, ["ts_code"])
            col = master["ts_code"]
            if hasattr(col, "drop_nulls"):
                _ETF_SYMBOL_CACHE.append(set(col.drop_nulls().to_list()))
            else:
                _ETF_SYMBOL_CACHE.append(set(col.dropna().tolist()))
    return _ETF_SYMBOL_CACHE[0]


def _group_by_asset(
        symbols: list[str], asset_type: AssetType | None = None) -> list[tuple[AssetType, list[str]]]:
    """按资产类型分组。asset_type 显式给出时直接使用, 否则用 ETF 维表判定 (其余归 stock)。

    index 不在本数据源覆盖范围 (alpha-factory 侧指数需另接), 显式返回空组,
    让面板把该阶段记为 0 行而不是整条盘后管道失败。
    """
    if asset_type == "index":
        return []
    if asset_type:
        return [(asset_type, list(symbols))]
    etf = _etf_symbol_set()
    etf_syms = [s for s in symbols if s in etf]
    stock_syms = [s for s in symbols if s not in etf]
    groups: list[tuple[AssetType, list[str]]] = []
    if etf_syms:
        groups.append(("etf", etf_syms))
    if stock_syms:
        groups.append(("stock", stock_syms))
    return groups


class _AfLocalConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset / list_sources 能识别本 provider。"""

    name = "af_local"
    display_name = "alpha-factory 本地"
    datasets = {"daily": {}, "adj_factor": {}}
    path = None
    builtin = True


class AfLocalProvider:
    """alpha-factory 本地 parquet provider (daily + adj_factor)。"""

    name = "af_local"
    config = _AfLocalConfig()
    capabilities = ProviderCapabilities(
        instruments=True,
        daily=True,
        adj_factor=True,
        minute=False,
        realtime=False,
        depth5=False,
        financial=False,
    )

    def close(self) -> None:
        return None

    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        if asset_type == "etf":
            _, master_path = _etf_paths()
            if not master_path.exists():
                raise ProviderRequestError(f"缺少 ETF 维表: {master_path}")
            master = _read_raw(master_path, ["ts_code", "csname", "list_date", "list_status", "exchange"])
            frame = master.rename({"ts_code": "symbol", "csname": "name", "list_date": "listing_date"}).with_columns(
                pl.col("listing_date").cast(pl.Utf8, strict=False).str.strptime(pl.Date, "%Y%m%d", strict=False),
                pl.lit("etf").alias("asset_type"),
                pl.lit(self.name).alias("source"),
            )
            return frame.select(["symbol", "name", "exchange", "asset_type", "source", "listing_date", "list_status"])
        if asset_type == "stock":
            cache_path = _ensure_cache(_stock_path(), "stock")
            frame = (
                pl.scan_parquet(cache_path)
                .select(pl.col("symbol").unique())
                .collect()
                .with_columns(
                    pl.lit(None, dtype=pl.Utf8).alias("name"),
                    pl.col("symbol").str.split(".").list.get(-1).alias("exchange"),
                    pl.lit("stock").alias("asset_type"),
                    pl.lit(self.name).alias("source"),
                    pl.lit(None, dtype=pl.Date).alias("listing_date"),
                    pl.lit(None, dtype=pl.Utf8).alias("list_status"),
                )
            )
            return frame.select(["symbol", "name", "exchange", "asset_type", "source", "listing_date", "list_status"])
        if asset_type == "index":
            # 指数不在本数据源覆盖范围; 返回空表让上层按 0 行处理, 不打断盘后管道。
            return pl.DataFrame(schema={
                "symbol": pl.Utf8, "name": pl.Utf8, "exchange": pl.Utf8,
                "asset_type": pl.Utf8, "source": pl.Utf8,
                "listing_date": pl.Date, "list_status": pl.Utf8,
            })
        raise ProviderRequestError(f"af_local 不提供 {asset_type} 维表")

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: AssetType | None = None,
        on_chunk_done: object | None = None,
    ) -> pl.DataFrame:
        if not symbols:
            return _empty_daily()
        frames: list[pl.DataFrame] = []
        for at, syms in _group_by_asset(symbols, asset_type):
            cache_path = _ensure_cache(_daily_source(at), at)
            start, end = _as_date(start_time), _as_date(end_time)
            lazy = pl.scan_parquet(cache_path).filter(pl.col("symbol").is_in(syms))
            if start is not None:
                lazy = lazy.filter(pl.col("date") >= pl.lit(start))
            if end is not None:
                lazy = lazy.filter(pl.col("date") <= pl.lit(end))
            frame = lazy.collect()
            if frame.is_empty():
                continue
            frames.append(
                frame.select(
                    [
                        "symbol",
                        pl.lit(at).alias("asset_type"),
                        pl.lit(self.name).alias("source"),
                        "date",
                        pl.col("open").cast(pl.Float64, strict=False),
                        pl.col("high").cast(pl.Float64, strict=False),
                        pl.col("low").cast(pl.Float64, strict=False),
                        pl.col("close").cast(pl.Float64, strict=False),
                        pl.col("volume").cast(pl.Float64, strict=False),
                        pl.col("amount").cast(pl.Float64, strict=False),
                        pl.col("pre_close").cast(pl.Float64, strict=False),
                        pl.col("change_pct").cast(pl.Float64, strict=False),
                    ]
                )
            )
        if not frames:
            return _empty_daily()
        return pl.concat(frames).sort(["symbol", "date"])

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: AssetType | None = None,
        on_chunk_done: object | None = None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame(schema={"symbol": pl.Utf8, "trade_date": pl.Date, "ex_factor": pl.Float64})
        frames: list[pl.DataFrame] = []
        for at, syms in _group_by_asset(symbols, asset_type):
            cache_path = _ensure_cache(_daily_source(at), at)
            start, end = _as_date(start_time), _as_date(end_time)
            raw = (
                pl.scan_parquet(cache_path)
                .filter(pl.col("symbol").is_in(syms) & pl.col("ex_factor").is_not_null())
                .select(
                    pl.col("symbol"),
                    pl.col("date").alias("trade_date"),
                    pl.col("ex_factor").cast(pl.Float64, strict=False),
                )
                .unique(subset=["symbol", "trade_date"], keep="last")
                .sort(["symbol", "trade_date"])
                .collect()
            )
            if raw.is_empty():
                continue
            # alpha-factory 的 adj_factor 是累积乘数; 面板 _apply_adj_factor 需要
            # 「每次除权事件的 pre/post 比值」(内部再 cum_prod)。这里按每只标的
            # 逐日相除还原事件比值, 并保留每只标的的首行作基线 (ratio=1), 否则
            # 窗口内早期日期在 join_asof 时取不到因子, 会被当成未复权。
            df = (
                raw.with_columns(pl.int_range(pl.len()).over("symbol").alias("_idx"),
                                 pl.col("ex_factor").shift(1).over("symbol").alias("_prev"))
                .with_columns(
                    pl.when(pl.col("_idx") == 0)
                    .then(1.0)
                    .otherwise(pl.col("ex_factor") / pl.col("_prev"))
                    .alias("ex_factor")
                )
            )
            baseline = df.filter(pl.col("_idx") == 0)
            events = df.filter((pl.col("_idx") > 0) & ((pl.col("ex_factor") - 1.0).abs() > 1e-6))
            if start is not None:
                events = events.filter(pl.col("trade_date") >= pl.lit(start))
            if end is not None:
                events = events.filter(pl.col("trade_date") <= pl.lit(end))
            frames.append(pl.concat([baseline, events]).select(
                "symbol", "trade_date", "ex_factor"))
        frames = [f for f in frames if not f.is_empty()]
        if not frames:
            return pl.DataFrame(schema={"symbol": pl.Utf8, "trade_date": pl.Date, "ex_factor": pl.Float64})
        return pl.concat(frames).sort(["symbol", "trade_date"])

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页「试拉」: 用给定标的或一只样本标的做最小可用性验证 (不写盘)。"""
        probe = list(symbols or [])
        if not probe:
            _, master_path = _etf_paths()
            master = _read_raw(master_path, ["ts_code"]).head(1)
            probe = master["ts_code"].to_list() or ["510300.SH"]
        sample = probe[0]
        if dataset == "daily":
            frame = self.get_daily(probe, None, None, "etf")
            return {"ok": frame.height > 0, "rows": frame.height,
                    "message": f"{len(probe)} 只标的共 {frame.height} 行日K",
                    "sample": frame.tail(3).to_dicts() if frame.height else []}
        if dataset == "adj_factor":
            frame = self.get_adj_factors(probe, None, None, "etf")
            return {"ok": frame.height > 0, "rows": frame.height,
                    "message": f"{len(probe)} 只标的共 {frame.height} 行复权因子",
                    "sample": frame.tail(3).to_dicts() if frame.height else []}
        return {"ok": False, "rows": 0, "message": f"af_local 不支持 {dataset}"}
