"""扩展数据 API — CRUD + 文件上传 + JSON 写入 + 定时拉取 + schema 发现。"""
from __future__ import annotations

import json
import logging
import math
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import polars as pl
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from app.db_safe import is_valid_ext_ident
from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    ExtField,
    PullConfig,
    ensure_utf8_csv,
    fix_symbol_format,
    normalize_symbol,
    rows_to_parquet,
    write_ext_parquet,
)
from app.services.ext_pull import fetch_and_ingest, pull_scheduler

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
    """根据 config 的 symbol_map / code_map 自动生成 symbol 和 code 列。
    
    执行顺序：先 mapped（从文件列复制），再 computed（从已生成的列计算）。
    """
    sm = config.symbol_map or {}
    cm = config.code_map or {}

    # --- 第一步：mapped 类型，直接从文件列映射 ---
    if sm.get("type") == "mapped" and sm["col"] in df.columns:
        df = df.with_columns(df[sm["col"]].cast(pl.Utf8).alias("symbol"))

    if cm.get("type") == "mapped" and cm["col"] in df.columns:
        df = df.with_columns(df[cm["col"]].cast(pl.Utf8).alias("code"))

    # --- 第二步：computed 类型，从已生成的列计算 ---
    if "symbol" not in df.columns and sm.get("type") == "computed":
        if sm.get("from") == "code" and "code" in df.columns:
            # code → symbol: 000001 → 000001.SZ
            from app.services.ext_data import build_code_lookup
            lookup = build_code_lookup(data_dir)
            df = df.with_columns(normalize_symbol(df["code"].cast(pl.Utf8), lookup).alias("symbol"))

    if "code" not in df.columns and cm.get("type") == "computed":
        if cm.get("from") == "symbol" and "symbol" in df.columns:
            # symbol → code: 000001.SZ → 000001
            df = df.with_columns(
                df["symbol"].cast(pl.Utf8).str.split(".").list.first().alias("code")
            )

    # --- 兜底：如果只有一个，自动生成另一个 ---
    if "symbol" in df.columns and "code" not in df.columns:
        df = df.with_columns(
            df["symbol"].cast(pl.Utf8).str.split(".").list.first().alias("code")
        )
    elif "code" in df.columns and "symbol" not in df.columns:
        from app.services.ext_data import build_code_lookup
        lookup = build_code_lookup(data_dir)
        df = df.with_columns(normalize_symbol(df["code"].cast(pl.Utf8), lookup).alias("symbol"))

    # 标准化 symbol 列
    if "symbol" in df.columns:
        from app.services.ext_data import build_code_lookup
        lookup = build_code_lookup(data_dir)
        df = df.with_columns(normalize_symbol(df["symbol"].cast(pl.Utf8), lookup))

    return df


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
        path = base / f"date={snapshot_date}" / "part.parquet"
        if not path.exists():
            return pl.DataFrame(), snapshot_date
        return pl.read_parquet(path), snapshot_date

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
    """扫描数据文件，返回该扩展配置的最新同步时间（含时分秒）。

    - snapshot: 直接取 ext_data/{id}/part.parquet 的 mtime
    - timeseries: 扫描 ext_data/{id}/timeseries/date=xxx 分区目录
    """
    from datetime import datetime

    if config.mode == "snapshot":
        # 快照: part.parquet 与 config.json 同级
        p = data_dir / "ext_data" / config.id / "part.parquet"
        if p.exists():
            ts = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
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
    """从 date=xxx 分区目录中找到最新分区的修改时间。"""
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
        ts = datetime.fromtimestamp(latest_ts).strftime("%H:%M:%S")
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
    return config.to_dict()


@router.delete("/{config_id}")
def delete_config(request: Request, config_id: str):
    """删除扩展数据配置。"""
    store = _store(request)
    if not store.delete(config_id):
        raise HTTPException(404, f"配置 '{config_id}' 不存在")
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


# ---------------------------------------------------------------------------
# 文件上传
# ---------------------------------------------------------------------------

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
        with tmp_path.open("wb") as f:
            content = await file.read()
            f.write(content)

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

    # 保留历史状态字段
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
        last_run=old_pull.last_run if old_pull else None,
        last_status=old_pull.last_status if old_pull else None,
        last_message=old_pull.last_message if old_pull else None,
        last_rows=old_pull.last_rows if old_pull else None,
    )
    store.upsert(config)

    # 刷新调度器
    pull_scheduler.refresh(_data_dir(request))

    return {"status": "ok", "pull": config.pull.to_dict()}


@router.post("/{config_id}/pull/test")
async def test_pull(request: Request, config_id: str):
    """测试拉取：请求外部 API 并返回预览数据，不写入。"""
    store = _store(request)
    config = store.get(config_id)
    if not config:
        raise HTTPException(404, f"配置 '{config_id}' 不存在")
    if not config.pull or not config.pull.url:
        raise HTTPException(400, "拉取未配置或 URL 为空")

    # 临时构建一个带新配置的 config 用于测试
    import httpx

    from app.services.ext_pull import _apply_field_map, _extract_rows

    pull = config.pull
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            headers = pull.headers or {}
            kwargs: dict = {"headers": headers}
            if pull.method.upper() == "POST" and pull.body:
                kwargs["content"] = pull.body
                if "content-type" not in {k.lower() for k in headers}:
                    kwargs["headers"]["Content-Type"] = "application/json"
            resp = await client.request(pull.method.upper(), pull.url, **kwargs)
            resp.raise_for_status()
            data = resp.json()

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
        return {"status": "ok", "rows": n, "date": d}
    except Exception as e:
        raise HTTPException(400, f"拉取失败: {e}") from e


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

_POLARS_TYPE_MAP = {
    "Int64": "int", "Int32": "int", "Int16": "int", "Int8": "int",
    "UInt64": "int", "UInt32": "int", "UInt16": "int", "UInt8": "int",
    "Float64": "float", "Float32": "float",
    "Boolean": "bool",
    "Utf8": "string", "String": "string",
    "Date": "string", "Datetime": "string", "Duration": "string",
    "Categorical": "string",
}


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
        with tmp_path.open("wb") as f:
            content = await file.read()
            f.write(content)

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

    # 构建字段列表
    fields = []
    for col_name in df.columns:
        pl_type = df[col_name].dtype
        dtype = _POLARS_TYPE_MAP.get(str(pl_type.base_type()), "string")
        fields.append({"name": col_name, "dtype": dtype, "label": col_name})

    # --- 分别检测 symbol 候选 (000001.SZ) 和 code 候选 (6位纯数字) ---
    import re
    _CODE_PAT = re.compile(r"^\d{6}$")
    _SYMBOL_PAT = re.compile(r"^\d{6}\.[A-Z]{2}$")

    symbol_candidates: list[str] = []   # 数据匹配 000001.SZ 格式
    code_candidates: list[str] = []     # 数据匹配 000001 格式

    for col in df.columns:
        col_data = df[col].cast(pl.Utf8).drop_nulls()
        if len(col_data) == 0:
            continue
        sample = col_data.head(200).to_list()
        sym_hits = sum(1 for v in sample if _SYMBOL_PAT.match(str(v).strip()))
        code_hits = sum(1 for v in sample if _CODE_PAT.match(str(v).strip()))
        total = len(sample)
        if total > 0:
            if sym_hits / total > 0.5:
                symbol_candidates.append(col)
            elif code_hits / total > 0.5:
                code_candidates.append(col)

    return {
        "fields": fields,
        "rows": len(df),
        "symbol_candidates": symbol_candidates,
        "code_candidates": code_candidates,
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
                cfg_id = raw.get("id")
                if cfg_id != cfg_dir.name or not is_valid_ext_ident(cfg_id):
                    logger.warning("跳过不安全的扩展表配置: %s", cp)
                    continue
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
