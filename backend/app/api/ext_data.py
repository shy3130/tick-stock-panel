"""扩展数据 API — CRUD + 文件上传 + JSON 写入 + 定时拉取 + schema 发现。"""
from __future__ import annotations

import json
import logging
import math
import re
import shutil
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import polars as pl
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from app.market_time import CN_TZ
from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    ExtField,
    PullConfig,
    apply_config_mapping,
    detect_symbol_candidates,
    ensure_utf8_csv,
    ext_api_key_field,
    fix_symbol_format,
    get_ext_api_key,
    infer_fields_from_df,
    parse_upload_file,
    write_ext_parquet,
    rows_to_parquet,
)
from app.services.ext_pull import _request_json, fetch_and_ingest, pull_scheduler

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ext-data", tags=["ext-data"])


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------

class FieldDef(BaseModel):
    name: str
    dtype: str = "string"        # string | int | float | bool
    label: str = ""


class CreateExtReq(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    label: str = Field(..., min_length=1, max_length=64)
    mode: Literal["snapshot", "timeseries"]
    fields: list[FieldDef] = Field(..., min_length=1)
    description: str = ""
    symbol_map: dict = {}   # {"type": "mapped", "col": "..."} 或 {"type": "computed", "from": "code", "method": "append_exchange"}
    code_map: dict = {}     # {"type": "mapped", "col": "..."} 或 {"type": "computed", "from": "symbol", "method": "strip_exchange"}


class UpdateExtReq(BaseModel):
    label: str | None = None
    fields: list[FieldDef] | None = None
    description: str | None = None
    symbol_map: dict | None = None
    code_map: dict | None = None


class IngestReq(BaseModel):
    """JSON 批量写入请求。"""
    date: str | None = None          # YYYY-MM-DD，不传默认今天
    rows: list[dict] = Field(..., min_length=1)


class PullAuthReq(BaseModel):
    """拉取接口鉴权方式 (与自定义行情源 AuthConfig 同口径)。

    Key 本体存 secrets_store (secrets.json), 不写入 config.json。
    """
    type: Literal["none", "bearer", "header", "query"] = "none"
    header: str = Field("Authorization", min_length=1, max_length=64)  # bearer/header 用
    param: str = Field("token", min_length=1, max_length=64)          # query 用


class PullConfigReq(BaseModel):
    """定时拉取配置请求。"""
    url: str = Field(..., min_length=1)
    method: str = "GET"
    headers: dict[str, str] | None = None
    body: str | None = None
    response_path: str = ""          # dot-path to rows array
    field_map: dict[str, str] | None = None  # external → internal field name
    schedule_minutes: int = Field(1440, ge=1)
    enabled: bool = False
    time_window_start: str | None = None   # "HH:MM", None=不限
    time_window_end: str | None = None     # "HH:MM", None=不限
    # 接口按日查询的参数名 (如 "date"): 配置后支持历史回补, 且当日拉取也带日期参数
    date_param: str | None = Field(None, min_length=1, max_length=16, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    # 日期参数值的格式: iso/compact/ts_s/ts_ms (时间戳=该交易日北京时间 00:00:00), 缺省 iso
    date_format: str = "iso"
    # 鉴权方式; 请求中缺省 (None) = 保留现有配置, {"type":"none"} = 关闭鉴权
    auth: PullAuthReq | None = None


class ApiKeyReq(BaseModel):
    """设置拉取接口 API Key; 空串 = 清除。"""
    key: str = Field(..., max_length=4096)


class DetectUrlReq(BaseModel):
    """URL 探测请求，不依赖已存在的扩展配置。"""
    url: str = Field(..., min_length=1)
    method: str = "GET"
    headers: dict[str, str] | None = None
    body: str | None = None
    response_path: str = ""
    field_map: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _store(request: Request) -> ExtConfigStore:
    return ExtConfigStore(request.app.state.repo.store.data_dir)


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _apply_mapping(df: pl.DataFrame, config: ExtConfig, data_dir: Path) -> pl.DataFrame:
    return apply_config_mapping(df, config, data_dir)


def _clean_col_names(df: pl.DataFrame) -> pl.DataFrame:
    """清洗列名：去掉所有 (...) 及其内容，避免时间戳导致列名不稳定。"""
    import re
    renames = {col: re.sub(r"\([^)]*\)", "", col).strip() for col in df.columns}
    # 去重：如果清洗后重名，加序号后缀
    seen: dict[str, int] = {}
    final = {}
    for old, new in renames.items():
        if new in seen:
            seen[new] += 1
            final[old] = f"{new}_{seen[new]}"
        else:
            seen[new] = 0
            final[old] = new
    return df.rename(final)


_DIMENSION_SEPARATOR_CLASS = r"、,，;；|/\s-"


def _filter_dimension_member_rows(df: pl.DataFrame, field: str, value: str) -> pl.DataFrame:
    """按分隔后的完整标签匹配成员，避免“人工智能”误命中“人工智能体”。"""
    if field not in df.columns:
        raise HTTPException(400, f"字段 '{field}' 不存在")
    normalized = value.strip()
    if not normalized:
        raise HTTPException(400, "标签值不能为空")
    pattern = rf"(^|[{_DIMENSION_SEPARATOR_CLASS}]){re.escape(normalized)}($|[{_DIMENSION_SEPARATOR_CLASS}])"
    return df.filter(
        pl.col(field)
        .cast(pl.String, strict=False)
        .fill_null("")
        .str.contains(pattern)
    )


def _ext_data_dir(config: ExtConfig, data_dir: Path) -> Path:
    """返回扩展数据的数据目录。

    - snapshot: data/ext_data/{id}/（part.parquet 与 config.json 同级）
    - timeseries: data/ext_data/{id}/timeseries/
    """
    cfg_dir = data_dir / "ext_data" / config.id
    if config.mode == "timeseries":
        return cfg_dir / "timeseries"
    return cfg_dir


def _parquet_glob(config: ExtConfig, data_dir: Path) -> str:
    """返回该扩展配置下所有 parquet 文件的 glob 模式。

    snapshot: 'data/ext_data/{id}/*.parquet'（只有 part.parquet）
    timeseries: 'data/ext_data/{id}/timeseries/**/*.parquet'
    """
    cfg_dir = data_dir / "ext_data" / config.id
    if config.mode == "snapshot":
        return str(cfg_dir / "*.parquet")
    return str(cfg_dir / "timeseries" / "**" / "*.parquet")


def _safe_json_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _partition_date(raw: str) -> str:
    """把 `date` 入参规范成 `YYYY-MM-DD` 分区名。

    这个值直接拼进分区目录名 (`timeseries/date=<value>`), 所以非法值不只是格式问题:
    `date=x/../../../../kline_daily` 会让读取路径离开 `ext_data/<id>/timeseries/`。
    同一文件的 `/sync`、`/ingest`、`/backfill` 都先 `date.fromisoformat` 再用, 只有
    `/rows` 和 `/dimension-members` 走的这条路把原始字符串直接拼进了路径。
    """
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as e:
        raise HTTPException(400, f"日期格式错误: {raw}") from e


def _read_ext_dataframe(
    config: ExtConfig,
    data_dir: Path,
    snapshot_date: str | None = None,
) -> tuple[pl.DataFrame, str | None]:
    cfg_dir = data_dir / "ext_data" / config.id

    if config.mode == "snapshot":
        path = cfg_dir / "part.parquet"
        if not path.exists():
            return pl.DataFrame(), None
        return pl.read_parquet(path), _latest_sync_date(config, data_dir)

    base = cfg_dir / "timeseries"
    if not base.exists():
        return pl.DataFrame(), None

    if snapshot_date:
        day = _partition_date(snapshot_date)
        path = base / f"date={day}" / "part.parquet"
        if not path.exists():
            return pl.DataFrame(), day
        return pl.read_parquet(path), day

    partitions = sorted(
        d for d in base.iterdir()
        if d.is_dir() and d.name.startswith("date=") and (d / "part.parquet").exists()
    )
    if not partitions:
        return pl.DataFrame(), None

    latest = partitions[-1]
    latest_date = latest.name[5:]
    return pl.read_parquet(latest / "part.parquet"), latest_date


def _with_instrument_name(df: pl.DataFrame, data_dir: Path) -> pl.DataFrame:
    if df.is_empty() or "symbol" not in df.columns or "name" in df.columns:
        return df
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        return df
    try:
        inst = pl.read_parquet(path)
        if "symbol" in inst.columns and "name" in inst.columns:
            inst = inst.select(["symbol", "name"]).unique(subset=["symbol"], keep="last")
            return df.join(inst, on="symbol", how="left")
    except Exception:
        return df
    return df


def _latest_sync_date(config: ExtConfig, data_dir: Path) -> str | None:
    """扫描数据文件，返回该扩展配置的最新同步时间（北京墙钟, 含时分秒）。

    - snapshot: 直接取 ext_data/{id}/part.parquet 的 mtime
    - timeseries: 扫描 ext_data/{id}/timeseries/date=xxx 分区目录

    用北京时间而非宿主机时钟: 前端 ExtDataStatCard 原样展示这串裸时间,
    容器默认 UTC 时会比同一页拉取面板里的 pull.last_run(带时区 ISO,
    浏览器按本地时区渲染)整整差一个时区。
    """
    from datetime import datetime

    if config.mode == "snapshot":
        # 快照: part.parquet 与 config.json 同级
        p = data_dir / "ext_data" / config.id / "part.parquet"
        if p.exists():
            ts = datetime.fromtimestamp(p.stat().st_mtime, tz=CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            return ts
        # 兼容旧路径
        old = data_dir / "instruments_ext"
        if old.exists():
            return _latest_sync_from_partitions(old)
        return None

    # 时序: 扫描 timeseries/date=xxx
    base = _ext_data_dir(config, data_dir)
    if not base.exists():
        # 兼容旧路径
        base = data_dir / "kline_ext"
    if not base.exists():
        return None
    return _latest_sync_from_partitions(base)


def _latest_sync_from_partitions(base: Path) -> str | None:
    """从 date=xxx 分区目录中找到最新分区的修改时间 (北京墙钟)。"""
    from datetime import datetime
    latest_ts: float = 0
    latest_date: str | None = None
    for d in base.iterdir():
        if d.is_dir() and d.name.startswith("date="):
            for f in d.glob("*.parquet"):
                mtime = f.stat().st_mtime
                if mtime > latest_ts:
                    latest_ts = mtime
                    latest_date = d.name[5:]
    if latest_date and latest_ts > 0:
        ts = datetime.fromtimestamp(latest_ts, tz=CN_TZ).strftime("%H:%M:%S")
        return f"{latest_date} {ts}"
    return latest_date


def _date_range(config: ExtConfig, data_dir: Path) -> list[str] | None:
    """返回时序型扩展数据的日期范围 [最早, 最新]。"""
    if config.mode != "timeseries":
        return None
    base = _ext_data_dir(config, data_dir)
    if not base.exists():
        # 兼容旧路径
        base = data_dir / "kline_ext"
    if not base.exists():
        return None
    dates: list[str] = []
    for d in base.iterdir():
        if d.is_dir() and d.name.startswith("date="):
            dates.append(d.name[5:])
    if len(dates) < 1:
        return None
    dates.sort()
    return [dates[0], dates[-1]]


@router.get("")
def list_configs(request: Request):
    """列出所有扩展数据配置。"""
    configs = _store(request).load_all()
    data_dir = _data_dir(request)
    items = []
    for c in configs:
        d = c.to_dict()
        d["latest_sync_date"] = _latest_sync_date(c, data_dir)
        d["date_range"] = _date_range(c, data_dir)
        items.append(d)
    return {"items": items}


@router.post("/presets/{config_id}/fetch")
async def fetch_preset_data(request: Request, config_id: str):
    """手动触发内置预设 (概念/行业) 的数据拉取。

    注意: 必须在 /{config_id}/... 动态路由之前声明, 否则 'presets' 会被当成 config_id。
    与通用 pull/run 不同: 走 ext_presets 的结构转换 (接口的 concepts/industries
    数组 → 拼接成字符串), 保证 schema 与现有数据一致。
    """
    from app.services.ext_presets import fetch_preset

    try:
        n = await fetch_preset(config_id, _data_dir(request))
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(400, f"拉取失败: {e}") from e

    _refresh_views(request)
    return {"status": "ok", "rows": n}


@router.post("")
def create_config(request: Request, body: CreateExtReq):
    """创建扩展数据配置。"""
    store = _store(request)
    if store.get(body.id):
        raise HTTPException(400, f"配置 '{body.id}' 已存在")
    config = ExtConfig(
        id=body.id,
        label=body.label,
        mode=body.mode,
        fields=[ExtField(f.name, f.dtype, f.label) for f in body.fields],
        description=body.description,
        symbol_map=body.symbol_map,
        code_map=body.code_map,
    )
    store.upsert(config)
    _refresh_views(request)
    return config.to_dict()


@router.put("/{config_id}")
def update_config(request: Request, config_id: str, body: UpdateExtReq):
    """更新扩展数据配置。"""
    store = _store(request)
    config = store.get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")
    if body.label is not None:
        config.label = body.label
    if body.fields is not None:
        config.fields = [ExtField(f.name, f.dtype, f.label) for f in body.fields]
    if body.description is not None:
        config.description = body.description
    if body.symbol_map is not None:
        config.symbol_map = body.symbol_map
    if body.code_map is not None:
        config.code_map = body.code_map
    store.upsert(config)
    _refresh_views(request)
    return config.to_dict()


@router.delete("/{config_id}")
def delete_config(request: Request, config_id: str):
    """删除扩展数据配置。"""
    store = _store(request)
    if not store.delete(config_id):
        raise HTTPException(404, f"配置 '{config_id}' 不存在")
    # 同步清掉 secrets.json 里残留的拉取 API Key, 避免同名重建配置时误用旧 Key
    from app import secrets_store

    secrets_store.clear(ext_api_key_field(config_id))
    _refresh_views(request)
    return {"status": "deleted"}


@router.get("/{config_id}/rows")
def list_rows(
    request: Request,
    config_id: str,
    snapshot_date: str | None = Query(None, alias="date"),
    columns: str | None = Query(None, description="逗号分隔的字段列表"),
    limit: int = Query(1000, ge=1, le=20000),
):
    """读取扩展数据明细。

    - snapshot: 返回当前快照。
    - timeseries: 默认返回最新日期分区，也可通过 date=YYYY-MM-DD 指定。
    """
    config = _store(request).get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")

    data_dir = _data_dir(request)
    df, active_date = _read_ext_dataframe(config, data_dir, snapshot_date)
    df = _with_instrument_name(df, data_dir)
    requested = [c.strip() for c in (columns or "").split(",") if c.strip()]
    if requested:
        keep = [c for c in ["symbol", "code", "name", *requested] if c in df.columns]
        if keep:
            df = df.select(list(dict.fromkeys(keep)))
    total = len(df)
    if total > limit:
        df = df.head(limit)

    rows = []
    for row in df.to_dicts():
        rows.append({k: _safe_json_value(v) for k, v in row.items()})

    return {
        "id": config.id,
        "label": config.label,
        "mode": config.mode,
        "date": active_date,
        "total": total,
        "limit": limit,
        "fields": [f.to_dict() for f in config.fields],
        "rows": rows,
    }


@router.get("/{config_id}/dimension-members")
def dimension_members(
    request: Request,
    config_id: str,
    field: str = Query(..., min_length=1),
    value: str = Query(..., min_length=1),
    snapshot_date: str | None = Query(None, alias="date"),
    limit: int = Query(1000, ge=1, le=10000),
):
    """按扩展字段的完整标签值返回成分股，不绑定具体概念/行业数据源。"""
    config = _store(request).get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")

    data_dir = _data_dir(request)
    df, active_date = _read_ext_dataframe(config, data_dir, snapshot_date)
    df = _with_instrument_name(df, data_dir)
    matched = _filter_dimension_member_rows(df, field, value)
    total = len(matched)

    columns = ["symbol", "code", "name", "股票代码", "股票简称", field]
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            columns.append(str(mapping["col"]))
    selected = [column for column in dict.fromkeys(columns) if column in matched.columns]
    if selected:
        matched = matched.select(selected)
    if total > limit:
        matched = matched.head(limit)

    symbol_columns = ["symbol", "code", "股票代码", "代码"]
    name_columns = ["name", "股票简称", "名称"]
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            symbol_columns.append(str(mapping["col"]))

    rows = []
    for raw in matched.to_dicts():
        row = {key: _safe_json_value(item) for key, item in raw.items()}
        if not row.get("symbol"):
            row["symbol"] = next((str(row[column]) for column in symbol_columns if row.get(column)), "")
        if not row.get("name"):
            row["name"] = next((str(row[column]) for column in name_columns if row.get(column)), "")
        rows.append(row)
    return {
        "id": config.id,
        "label": config.label,
        "date": active_date,
        "field": field,
        "value": value.strip(),
        "total": total,
        "limit": limit,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 板块分时 (dimension intraday)
# ---------------------------------------------------------------------------

# 点击触发 + 60s 进程内缓存: 分钟分区是滚动底座 (minute_refresh / 盘后分钟同步),
# 不做后台预计算 — 板块基数大而单次聚合仅几十毫秒。
_DIMENSION_INTRADAY_CACHE: dict[tuple[str, str, str, str | None], tuple[float, dict]] = {}
_DIMENSION_INTRADAY_CACHE_TTL_S = 60.0
# 成分股网格化 ffill 上限: 超大板块退化为逐时间戳可得均值 (内存保护)。
_DIMENSION_INTRADAY_FFILL_CAP = 2000


def _bare_symbol_expr(col: str = "symbol") -> pl.Expr:
    """'000001.SZ' → '000001'; 已是裸代码则原样。"""
    return pl.col(col).cast(pl.String).str.strip_chars().str.split(".").list.first()


def _dimension_member_bares(matched: pl.DataFrame, config: ExtConfig) -> list[str]:
    """成分股裸代码集合 (symbol 列优先级与 dimension-members 端点一致)。"""
    if matched.is_empty():
        return []
    symbol_columns = ["symbol", "code", "股票代码", "代码"]
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            symbol_columns.append(str(mapping["col"]))
    cols = [c for c in dict.fromkeys(symbol_columns) if c in matched.columns]
    if not cols:
        return []
    coalesced = pl.coalesce(
        [pl.col(c).cast(pl.String).str.strip_chars().str.split(".").list.first() for c in cols]
    )
    series = matched.select(coalesced.alias("_bare")).to_series()
    return sorted({s for s in series.to_list() if s})


def _prev_daily_close(data_dir: Path, target_date: str) -> pl.DataFrame | None:
    """目标日前最近一个日K分区的收盘价 → (_bare, prev_close); 无则 None。"""
    daily = data_dir / "kline_daily"
    if not daily.exists():
        return None
    dates = sorted(
        d.name[5:]
        for d in daily.iterdir()
        if d.is_dir() and d.name.startswith("date=") and (d / "part.parquet").exists()
    )
    prevs = [d for d in dates if d < target_date]
    if not prevs:
        return None
    path = daily / f"date={prevs[-1]}" / "part.parquet"
    if not path.exists():
        return None
    df = pl.read_parquet(path, columns=["symbol", "close"])
    return (
        df.with_columns(_bare_symbol_expr().alias("_bare"))
        .select([pl.col("_bare"), pl.col("close").cast(pl.Float64).alias("prev_close")])
        .unique(subset=["_bare"], keep="last")
    )


def _dimension_intraday_compute(
    config: ExtConfig,
    data_dir: Path,
    field: str,
    value: str,
    snapshot_date: str | None,
) -> dict:
    """板块等权分时: 成分股当日分钟K逐分钟平均涨跌幅 + 全市场对照线。

    口径: pct = 分钟close / ref − 1 (小数制, 与快照涨跌幅契约一致, 前端 ×100 显示),
    ref 优先前一交易日日K收盘 (prev_close, 开盘跳空体现在曲线起点);
    日K缺失的标的退化为当日首根分钟close (混合基准)。
    停牌/无成交分钟按成分股 forward-fill 后再平均, 全市场线取逐时间戳可得均值。
    """
    minute_dir = data_dir / "kline_minute"
    partitions: list[str] = []
    if minute_dir.exists():
        partitions = sorted(
            d.name[5:]
            for d in minute_dir.iterdir()
            if d.is_dir() and d.name.startswith("date=") and (d / "part.parquet").exists()
        )
    if snapshot_date:
        target = snapshot_date if snapshot_date in partitions else None
    else:
        target = partitions[-1] if partitions else None
    if not target:
        return {"status": "no_data", "reason": "minute_missing", "date": snapshot_date, "points": []}

    ext_df, _active = _read_ext_dataframe(config, data_dir)
    if ext_df.is_empty() or field not in ext_df.columns:
        return {"status": "empty", "reason": "no_members", "date": target, "points": []}
    member_bares = _dimension_member_bares(_filter_dimension_member_rows(ext_df, field, value), config)
    if not member_bares:
        return {"status": "empty", "reason": "no_members", "date": target, "points": []}

    try:
        bars = pl.read_parquet(
            minute_dir / f"date={target}" / "part.parquet",
            columns=["symbol", "datetime", "close"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dimension-intraday read minute partition failed: %s", exc)
        return {"status": "no_data", "reason": "minute_schema", "date": target, "points": []}
    bars = bars.drop_nulls(subset=["datetime", "close"])
    if bars.is_empty():
        return {"status": "no_data", "reason": "minute_empty", "date": target, "points": []}
    bars = bars.with_columns(_bare_symbol_expr().alias("_bare"))

    prev = _prev_daily_close(data_dir, target)
    joined = bars.join(prev, on="_bare", how="left") if prev is not None else bars.with_columns(
        pl.lit(None, dtype=pl.Float64).alias("prev_close")
    )
    refs = joined.group_by("_bare").agg(
        pl.col("prev_close").first().alias("_prev"),
        pl.col("close").sort_by("datetime").first().alias("_first"),
    ).with_columns(pl.coalesce(["_prev", "_first"]).alias("_ref"))
    n_prev = refs["_prev"].is_not_null().sum()
    basis = "prev_close" if n_prev == refs.height else ("first_close" if n_prev == 0 else "mixed")
    joined = (
        joined.join(refs.select(["_bare", "_ref"]), on="_bare", how="left")
        .with_columns((pl.col("close") / pl.col("_ref") - 1.0).alias("_pct"))
    )

    market = joined.group_by("datetime").agg(pl.col("_pct").mean().alias("_market"))

    member_bars = joined.filter(pl.col("_bare").is_in(member_bares))
    members_with_minute = member_bars["_bare"].n_unique() if not member_bars.is_empty() else 0
    if members_with_minute == 0:
        return {
            "status": "empty", "reason": "no_member_bars", "date": target,
            "member_count": len(member_bares), "members_with_minute": 0, "points": [],
        }
    if members_with_minute <= _DIMENSION_INTRADAY_FFILL_CAP:
        # 网格化 (成分股 × 全时间轴) + 逐股 ffill: 停牌分钟冻结在最后价而非退出均值
        grid = (
            member_bars.select(pl.col("_bare").unique())
            .join(joined.select(pl.col("datetime").unique()), how="cross")
        )
        member_bars = (
            grid.join(member_bars.select(["_bare", "datetime", "_pct"]), on=["_bare", "datetime"], how="left")
            .sort(["_bare", "datetime"])
            .with_columns(pl.col("_pct").forward_fill().over("_bare"))
        )
    sector = member_bars.group_by("datetime").agg(pl.col("_pct").mean().alias("_sector"))

    combined = market.join(sector, on="datetime", how="left").sort("datetime")

    def _r4(v) -> float | None:
        return round(float(v), 4) if v is not None and not (isinstance(v, float) and math.isnan(v)) else None

    points = [
        {
            "time": row["datetime"].strftime("%H:%M"),
            "sector": _r4(row["_sector"]),
            "market": _r4(row["_market"]),
        }
        for row in combined.iter_rows(named=True)
    ]
    return {
        "status": "ok",
        "date": target,
        "basis": basis,
        "member_count": len(member_bares),
        "members_with_minute": members_with_minute,
        "points": points,
    }


@router.get("/{config_id}/dimension-intraday")
def dimension_intraday(
    request: Request,
    config_id: str,
    field: str = Query(..., min_length=1),
    value: str = Query(..., min_length=1),
    snapshot_date: str | None = Query(None, alias="date"),
):
    """板块分时走势 (等权): 成分股 × 当日分钟K聚合; 60s 缓存, 点击触发不预计算。"""
    config = _store(request).get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")

    cache_key = (config_id, field, value.strip(), snapshot_date)
    now = time.monotonic()
    hit = _DIMENSION_INTRADAY_CACHE.get(cache_key)
    if hit is not None and now - hit[0] < _DIMENSION_INTRADAY_CACHE_TTL_S:
        return hit[1]

    payload = _dimension_intraday_compute(config, _data_dir(request), field, value, snapshot_date)
    _DIMENSION_INTRADAY_CACHE[cache_key] = (now, payload)
    return payload


# ---------------------------------------------------------------------------
# 文件上传
# ---------------------------------------------------------------------------

# 扩展数据 CSV/Excel 上传上限(与自选截图 OCR 的 12MB 上限属同类保护, 见 watchlist.py)。
# 通过分块写入临时文件, 超限即拒绝, 避免 `await file.read()` 把整个文件读入内存。
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _write_upload_capped(file: UploadFile, dest: Path, max_bytes: int) -> None:
    """分块把上传文件写入 dest, 累计超过 max_bytes 立即拒绝(413)。

    避免一次性 `await file.read()` 把整个文件读入内存(大文件可能触发高内存占用、
    进程 OOM 或服务不可用); 超限时停止继续读取与落盘。
    """
    total = 0
    with dest.open("wb") as f:
        while True:
            chunk = await file.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(413, f"文件过大(上限 {max_bytes // (1024 * 1024)}MB)")
            f.write(chunk)


@router.post("/{config_id}/upload")
async def upload_data(
    request: Request,
    config_id: str,
    file: UploadFile = File(...),
    snapshot_date: str | None = None,
):
    """上传 CSV/Excel 文件写入扩展数据。"""
    store = _store(request)
    config = store.get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")

    # 校验文件后缀
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".csv", ".xlsx", ".xls"):
        raise HTTPException(400, "仅支持 CSV / Excel 文件")

    # 写到临时文件再解析
    tmp_dir = Path(tempfile.mkdtemp())
    tmp_path = tmp_dir / f"upload{suffix}"
    try:
        await _write_upload_capped(file, tmp_path, _MAX_UPLOAD_BYTES)

        # 直接读取文件，不做列重命名
        if suffix == ".csv":
            df = pl.read_csv(ensure_utf8_csv(tmp_path), infer_schema_length=10000)
        elif suffix in (".xlsx", ".xls"):
            df = pl.read_excel(tmp_path)
        else:
            raise HTTPException(400, f"不支持的文件格式: {suffix}")

        # 清洗列名：去掉括号内的时间戳等信息
        df = _clean_col_names(df)

        # 按映射关系自动生成 symbol 和 code 列
        df = _apply_mapping(df, config, _data_dir(request))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 确保配置的字段列存在于上传数据中（symbol/code 由映射自动生成，不校验）
    auto_fields = {"symbol", "code"}
    config_cols = {f.name for f in config.fields} - auto_fields
    missing = config_cols - set(df.columns)
    if missing:
        raise HTTPException(400, f"上传数据缺少字段: {', '.join(sorted(missing))}")

    # 只保留配置中定义的列（包括自动生成的 symbol/code），忽略文件中多余的字段
    all_config_cols = {f.name for f in config.fields}
    keep = [c for c in df.columns if c in all_config_cols]
    df = df.select(keep)

    # 解析快照日期
    snap = date.fromisoformat(snapshot_date) if snapshot_date else date.today()

    rows = write_ext_parquet(df, config, _data_dir(request), snapshot_date=snap)

    # 刷新 DuckDB 视图
    _refresh_views(request)

    return {"status": "ok", "rows": rows, "date": snap.isoformat()}


# ---------------------------------------------------------------------------
# JSON 接口写入
# ---------------------------------------------------------------------------

@router.post("/{config_id}/ingest")
def ingest_data(request: Request, config_id: str, body: IngestReq):
    """通过 JSON 接口批量写入扩展数据。"""
    store = _store(request)
    config = store.get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")

    # 校验必填字段
    configured = {f.name for f in config.fields}
    required = configured - {"symbol"}
    for i, row in enumerate(body.rows):
        if "symbol" not in row:
            raise HTTPException(400, f"第 {i + 1} 行缺少 symbol 字段")
        missing = required - set(row.keys())
        if missing:
            raise HTTPException(400, f"第 {i + 1} 行缺少字段: {', '.join(sorted(missing))}")

    snap = date.fromisoformat(body.date) if body.date else date.today()

    rows_written = rows_to_parquet(body.rows, config, _data_dir(request), snapshot_date=snap)

    _refresh_views(request)

    return {"status": "ok", "rows": rows_written, "date": snap.isoformat()}


# ---------------------------------------------------------------------------
# 定时拉取
# ---------------------------------------------------------------------------

@router.put("/{config_id}/pull")
def configure_pull(request: Request, config_id: str, body: PullConfigReq):
    """配置（或更新）定时拉取。"""
    store = _store(request)
    config = store.get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")

    # 保留历史状态字段; auth 缺省时沿用现有配置 (关闭鉴权需显式传 {"type":"none"})
    old_pull = config.pull
    config.pull = PullConfig(
        url=body.url,
        method=body.method,
        headers=body.headers,
        body=body.body,
        response_path=body.response_path,
        field_map=body.field_map,
        schedule_minutes=body.schedule_minutes,
        enabled=body.enabled,
        time_window_start=body.time_window_start,
        time_window_end=body.time_window_end,
        date_param=body.date_param,
        date_format=body.date_format,
        auth=body.auth.model_dump() if body.auth else (old_pull.auth if old_pull else None),
        last_run=old_pull.last_run if old_pull else None,
        last_status=old_pull.last_status if old_pull else None,
        last_message=old_pull.last_message if old_pull else None,
        last_rows=old_pull.last_rows if old_pull else None,
    )
    store.upsert(config)

    # 刷新调度器
    pull_scheduler.refresh(_data_dir(request))

    # 关闭定时拉取时清理残留的 next_run, 避免前端展示一个永不执行的"下次"
    if not config.pull.enabled:
        cleared = store.get(config_id)
        if cleared and cleared.pull and cleared.pull.next_run:
            cleared.pull.next_run = None
            store.upsert(cleared)

    return {"status": "ok", "pull": config.pull.to_dict()}


@router.get("/{config_id}/api-key")
def get_pull_api_key(request: Request, config_id: str):
    """查询拉取接口 API Key 状态。只返回脱敏值, 不返回明文。"""
    store = _store(request)
    config = store.get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")

    from app import secrets_store

    key = get_ext_api_key(config_id)
    return {"key_set": bool(key), "masked_key": secrets_store.mask(key) if key else ""}


@router.put("/{config_id}/api-key")
def set_pull_api_key(request: Request, config_id: str, body: ApiKeyReq):
    """设置 (或空串清除) 拉取接口的 API Key, 存 secrets.json (权限 0600)。"""
    store = _store(request)
    config = store.get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")

    from app import secrets_store

    value = body.key.strip()
    if value:
        secrets_store.save({ext_api_key_field(config_id): value})
    else:
        secrets_store.clear(ext_api_key_field(config_id))
    return {"status": "ok", "key_set": bool(value), "masked_key": secrets_store.mask(value) if value else ""}


@router.post("/{config_id}/pull/test")
async def test_pull(request: Request, config_id: str):
    """测试拉取：请求外部 API 并返回预览数据，不写入。"""
    store = _store(request)
    config = store.get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")
    if not config.pull or not config.pull.url:
        raise HTTPException(400, "拉取未配置或 URL 为空")

    # 复用正式拉取的请求实现 (UA 标识头 + 鉴权注入同一套口径), 不带日期参数
    from app.services.ext_pull import _apply_field_map, _extract_rows

    pull = config.pull
    try:
        data = await _request_json(pull, config.id)
        rows = _extract_rows(data, pull.response_path)
        preview = _apply_field_map(rows[:5], pull.field_map)
        return {
            "status": "ok",
            "total_rows": len(rows),
            "preview": preview,
            "has_symbol": bool(rows and "symbol" in rows[0]),
        }
    except Exception as e:
        raise HTTPException(400, f"测试失败: {e}") from e


@router.post("/{config_id}/pull/run")
async def run_pull(request: Request, config_id: str):
    """手动触发一次拉取并写入。"""
    store = _store(request)
    config = store.get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")
    if not config.pull or not config.pull.url:
        raise HTTPException(400, "拉取未配置或 URL 为空")

    try:
        n, d = await fetch_and_ingest(config, _data_dir(request))
        _refresh_views(request)
        # 写回执行状态, 让前端"上次执行"面板立即反映
        updated = store.get(config_id)
        if updated and updated.pull:
            from datetime import datetime, timezone
            updated.pull.last_run = datetime.now(timezone.utc).isoformat()
            updated.pull.last_status = "success"
            updated.pull.last_message = f"{n} rows @ {d}"
            updated.pull.last_rows = n
            store.upsert(updated)
        return {"status": "ok", "rows": n, "date": d}
    except Exception as e:
        # 失败也写回状态, 记录错误信息
        failed = store.get(config_id)
        if failed and failed.pull:
            from datetime import datetime, timezone
            failed.pull.last_run = datetime.now(timezone.utc).isoformat()
            failed.pull.last_status = "error"
            failed.pull.last_message = str(e)[:200]
            store.upsert(failed)
        raise HTTPException(400, f"拉取失败: {e}") from e


@router.post("/{config_id}/backfill")
async def backfill_history_ep(
    request: Request,
    config_id: str,
    start: str = Query(..., description="开始日期 YYYY-MM-DD"),
    end: str = Query(..., description="结束日期 YYYY-MM-DD (含)"),
):
    """历史回补: 按本地交易日逐日拉取并写入 timeseries 分区。

    前提: 配置为 timeseries 模式且拉取配置了 date_param (接口支持按日期
    查询)。幂等 —— 已存在的分区跳过, 失败单日不中断, 结果逐项返回。
    """
    store = _store(request)
    config = store.get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError as e:
        raise HTTPException(422, f"日期格式错误 (应为 YYYY-MM-DD): {e}") from e

    from app.services.ext_pull import backfill_history

    try:
        result = await backfill_history(config, _data_dir(request), start_d, end_d)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _refresh_views(request)
    return {"status": "ok", **result}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Symbol 格式修复
# ---------------------------------------------------------------------------

@router.post("/{config_id}/fix-symbol")
def fix_symbol(request: Request, config_id: str):
    """扫描已有 Parquet 数据，将 symbol 列标准化为 代码.交易所 格式。"""
    store = _store(request)
    config = store.get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")

    fixed = fix_symbol_format(config, _data_dir(request))
    _refresh_views(request)
    return {"status": "ok", "fixed_files": fixed}


# ---------------------------------------------------------------------------
# Schema 发现
# ---------------------------------------------------------------------------


@router.post("/detect-fields")
async def detect_fields(
    request: Request,
    file: UploadFile = File(...),
):
    """上传 CSV/Excel 文件，自动检测列名和类型。

    返回 symbol_candidates（数据匹配 000001.SZ 格式的列）和
    code_candidates（数据匹配 6位纯数字的列）。
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".csv", ".xlsx", ".xls"):
        raise HTTPException(400, "仅支持 CSV / Excel 文件")

    tmp_dir = Path(tempfile.mkdtemp())
    tmp_path = tmp_dir / f"upload{suffix}"
    try:
        await _write_upload_capped(file, tmp_path, _MAX_UPLOAD_BYTES)

        # 直接读取，不要求 symbol 列
        if suffix == ".csv":
            df = pl.read_csv(ensure_utf8_csv(tmp_path), infer_schema_length=10000)
        elif suffix in (".xlsx", ".xls"):
            df = pl.read_excel(tmp_path)
        else:
            raise HTTPException(400, f"不支持的文件格式: {suffix}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 清洗列名：去掉括号内的时间戳等信息
    df = _clean_col_names(df)
    symbol_candidates, code_candidates = detect_symbol_candidates(df)

    return {
        "fields": infer_fields_from_df(df),
        "rows": len(df),
        "symbol_candidates": symbol_candidates,
        "code_candidates": code_candidates,
    }


def _find_row_arrays(data, prefix: str = "", limit: int = 8) -> list[str]:
    """自动寻找 JSON 中可能的数据数组路径。"""
    found: list[str] = []

    def walk(value, path: str) -> None:
        if len(found) >= limit:
            return
        if isinstance(value, list):
            if value and isinstance(value[0], dict):
                found.append(path)
            elif value and isinstance(value[0], list):
                for i, item in enumerate(value[:3]):
                    walk(item, f"{path}.{i}" if path else str(i))
        elif isinstance(value, dict):
            for key, child in value.items():
                next_path = f"{path}.{key}" if path else key
                walk(child, next_path)

    walk(data, prefix)
    return found


@router.post("/detect-url")
async def detect_url(body: DetectUrlReq):
    """请求外部 URL，自动检测 JSON 行数据的字段和标的代码列。"""
    from app.services.ext_pull import _extract_rows, _apply_field_map
    import httpx

    method = body.method.upper()
    if method not in ("GET", "POST"):
        raise HTTPException(400, "仅支持 GET / POST")

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            headers = body.headers or {}
            kwargs: dict = {"headers": headers}
            if method == "POST" and body.body:
                kwargs["content"] = body.body
                if "content-type" not in {k.lower() for k in headers}:
                    kwargs["headers"]["Content-Type"] = "application/json"
            resp = await client.request(method, body.url, **kwargs)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise HTTPException(400, f"URL 请求失败: {e}") from e

    path_candidates = _find_row_arrays(data)
    response_path = body.response_path
    if not response_path:
        if not path_candidates:
            raise HTTPException(400, "未在响应中找到对象数组，请填写响应数据路径")
        response_path = path_candidates[0]

    try:
        rows = _extract_rows(data, response_path)
        rows = _apply_field_map(rows, body.field_map or {})
    except Exception as e:
        raise HTTPException(400, f"响应解析失败: {e}") from e

    if not rows:
        raise HTTPException(400, "提取到的行数为 0")
    if not all(isinstance(row, dict) for row in rows[:200]):
        raise HTTPException(400, "响应数据数组中的元素必须是对象")

    sample_rows = rows[: min(len(rows), 500)]
    try:
        df = pl.DataFrame(sample_rows)
    except Exception as e:
        raise HTTPException(400, f"样例数据解析失败: {e}") from e

    df = _clean_col_names(df)
    symbol_candidates, code_candidates = detect_symbol_candidates(df)
    preview = [
        {k: _safe_json_value(v) for k, v in row.items()}
        for row in df.head(10).to_dicts()
    ]

    return {
        "status": "ok",
        "total_rows": len(rows),
        "response_path": response_path,
        "response_path_candidates": path_candidates,
        "fields": infer_fields_from_df(df),
        "symbol_candidates": symbol_candidates,
        "code_candidates": code_candidates,
        "preview": preview,
    }


@router.get("/schema/{config_id}")
def discover_schema(request: Request, config_id: str):
    """发现扩展数据的实际 Parquet schema（基于已有数据）。"""
    config = _store(request).get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")

    data_dir = _data_dir(request)
    glob = _parquet_glob(config, data_dir)

    try:
        import duckdb
        rows = duckdb.query(
            f"SELECT column_name, data_type FROM (DESCRIBE SELECT * FROM read_parquet('{glob}', union_by_name=true))"
        ).fetchall()
        return {"columns": [{"name": r[0], "type": r[1]} for r in rows]}
    except Exception:
        # 无数据时返回配置中定义的字段
        return {"columns": [f.to_dict() for f in config.fields]}


@router.get("/schema-all")
def discover_all_schemas(request: Request):
    """发现所有扩展表的 schema（用于前端动态列选择）。"""
    configs = _store(request).load_all()
    result = []
    for config in configs:
        data_dir = _data_dir(request)
        glob = _parquet_glob(config, data_dir)

        try:
            import duckdb
            cols = duckdb.query(
                f"SELECT column_name, data_type FROM (DESCRIBE SELECT * FROM read_parquet('{glob}', union_by_name=true))"
            ).fetchall()
            field_labels = {f.name: f.label for f in config.fields}
            columns = [{"name": r[0], "type": r[1], "label": field_labels.get(r[0], r[0])} for r in cols]
        except Exception:
            columns = [f.to_dict() for f in config.fields]

        result.append({
            "id": config.id,
            "label": config.label,
            "mode": config.mode,
            "columns": columns,
        })
    return {"items": result}


# ---------------------------------------------------------------------------
# 视图刷新
# ---------------------------------------------------------------------------

def _refresh_views(request: Request) -> None:
    """重新注册 DuckDB 视图以包含新的扩展数据。"""
    repo = request.app.state.repo
    db = repo.store.db
    d = repo.store.data_dir.as_posix()

    # 注册旧路径视图（兼容）
    for name, subdir in [("instruments_ext", "instruments_ext"), ("kline_ext", "kline_ext")]:
        old_glob = f"{d}/{subdir}/**/*.parquet"
        old_dir = Path(d) / subdir
        if old_dir.exists():
            sql = (
                f"CREATE OR REPLACE VIEW {name} AS "
                f"SELECT * FROM read_parquet('{old_glob}', union_by_name=true)"
            )
            try:
                db.execute(sql)
            except Exception:
                pass

    # 注册新路径视图：每个扩展表一个视图 ext_{config_id}
    ext_base = Path(d) / "ext_data"
    if ext_base.exists():
        for cfg_dir in ext_base.iterdir():
            if not cfg_dir.is_dir():
                continue
            cp = cfg_dir / "config.json"
            if not cp.exists():
                continue
            try:
                raw = json.loads(cp.read_text(encoding="utf-8"))
                cfg_id = raw["id"]
                # 检查是否有数据文件（snapshot: part.parquet, timeseries: timeseries/ 目录）
                has_data = (cfg_dir / "part.parquet").exists() or (cfg_dir / "timeseries").exists()
                if has_data:
                    view_name = f"ext_{cfg_id}"
                    # snapshot: part.parquet 在 cfg_dir/ 根下; timeseries: 在 timeseries/ 子目录
                    mode = raw.get("mode", "snapshot")
                    if mode == "snapshot":
                        glob_pattern = f"{cfg_dir.as_posix()}/*.parquet"
                    else:
                        glob_pattern = f"{cfg_dir.as_posix()}/timeseries/**/*.parquet"
                    sql = (
                        f"CREATE OR REPLACE VIEW {view_name} AS "
                        f"SELECT * FROM read_parquet('{glob_pattern}', union_by_name=true)"
                    )
                    db.execute(sql)
            except Exception:
                pass

    # 扩展列已接入 enriched 帧 (compute_signals/compute_enriched_today 注入):
    # repo 内存 enriched 缓存 (_enriched_cache/_etf_/_index_) 持有含旧扩展列的
    # 帧, 必须一并清理, 否则写入后监控/列表仍用旧值 (服务层已清扩展帧与策略缓存)。
    if hasattr(repo, "clear_cache"):
        repo.clear_cache()
