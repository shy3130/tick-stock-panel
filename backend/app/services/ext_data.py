"""扩展数据服务 — 配置管理 + 文件解析 + Parquet 存储。"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import polars as pl

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------

class ExtField:
    """扩展字段定义。"""
    __slots__ = ("name", "dtype", "label")

    def __init__(self, name: str, dtype: str = "string", label: str = "") -> None:
        self.name = name
        self.dtype = dtype      # string | int | float | bool
        self.label = label or name

    def to_dict(self) -> dict:
        return {"name": self.name, "dtype": self.dtype, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict) -> ExtField:
        return cls(d["name"], d.get("dtype", "string"), d.get("label", ""))


class PullConfig:
    """定时拉取配置。"""
    __slots__ = (
        "url", "method", "headers", "body", "response_path",
        "field_map", "schedule_minutes", "enabled",
        "last_run", "last_status", "last_message", "last_rows",
        "next_run",
    )

    def __init__(
        self,
        url: str = "",
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
        response_path: str = "",
        field_map: dict[str, str] | None = None,
        schedule_minutes: int = 1440,
        enabled: bool = False,
        last_run: str | None = None,
        last_status: str | None = None,
        last_message: str | None = None,
        last_rows: int | None = None,
        next_run: str | None = None,
    ) -> None:
        self.url = url
        self.method = method              # GET | POST
        self.headers = headers or {}
        self.body = body                  # JSON string (POST body template)
        self.response_path = response_path  # dot-path to rows array, e.g. "data.list"
        self.field_map = field_map or {}    # external_name → config_field_name
        self.schedule_minutes = schedule_minutes
        self.enabled = enabled
        self.last_run = last_run
        self.last_status = last_status      # "success" | "error"
        self.last_message = last_message
        self.last_rows = last_rows
        self.next_run = next_run            # 下次预计运行 (ISO, 调度器写入)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "method": self.method,
            "headers": self.headers,
            "body": self.body,
            "response_path": self.response_path,
            "field_map": self.field_map,
            "schedule_minutes": self.schedule_minutes,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "last_status": self.last_status,
            "last_message": self.last_message,
            "last_rows": self.last_rows,
            "next_run": self.next_run,
        }

    @classmethod
    def from_dict(cls, d: dict) -> PullConfig:
        if not d:
            return cls()
        return cls(
            url=d.get("url", ""),
            method=d.get("method", "GET"),
            headers=d.get("headers"),
            body=d.get("body"),
            response_path=d.get("response_path", ""),
            field_map=d.get("field_map"),
            schedule_minutes=d.get("schedule_minutes", 1440),
            enabled=d.get("enabled", False),
            last_run=d.get("last_run"),
            last_status=d.get("last_status"),
            last_message=d.get("last_message"),
            last_rows=d.get("last_rows"),
            next_run=d.get("next_run"),
        )


class ExtConfig:
    """一个扩展数据源的完整配置。"""
    __slots__ = (
        "id", "label", "mode", "fields", "description",
        "symbol_map", "code_map",
        "created_at", "updated_at", "pull",
    )

    def __init__(
        self,
        id: str,
        label: str,
        mode: Literal["snapshot", "timeseries"],
        fields: list[ExtField],
        description: str = "",
        symbol_map: dict | None = None,
        code_map: dict | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        pull: PullConfig | None = None,
    ) -> None:
        self.id = id
        self.label = label
        self.mode = mode
        self.fields = fields
        self.description = description
        # 映射关系: {"type": "mapped", "col": "原始列名"} 或 {"type": "computed", "from": "symbol|code", "method": "strip_exchange|append_exchange"}
        self.symbol_map = symbol_map or {}
        self.code_map = code_map or {}
        self.created_at = created_at or datetime.now().isoformat()
        self.updated_at = updated_at or datetime.now().isoformat()
        self.pull = pull

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "label": self.label,
            "mode": self.mode,
            "fields": [f.to_dict() for f in self.fields],
            "description": self.description,
            "symbol_map": self.symbol_map,
            "code_map": self.code_map,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.pull:
            d["pull"] = self.pull.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ExtConfig:
        return cls(
            id=d["id"],
            label=d["label"],
            mode=d["mode"],
            fields=[ExtField.from_dict(f) for f in d.get("fields", [])],
            description=d.get("description", ""),
            symbol_map=d.get("symbol_map"),
            code_map=d.get("code_map"),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            pull=PullConfig.from_dict(d["pull"]) if d.get("pull") else None,
        )


# ---------------------------------------------------------------------------
# 配置持久化
# ---------------------------------------------------------------------------

class ExtConfigStore:
    """扩展数据配置文件读写 — 每个表独立目录 data/ext/{config_id}/config.json。"""

    def __init__(self, data_dir: Path) -> None:
        self._base = data_dir / "ext_data"

    def _config_path(self, config_id: str) -> Path:
        return self._base / config_id / "config.json"

    def load_all(self) -> list[ExtConfig]:
        # 兼容旧版: 如果目录为空且旧配置文件存在则迁移
        if not self._base.exists() or not any(self._base.iterdir()):
            old = self._base.parent / "ext_configs.json"
            if not old.exists():
                old = self._base.parent / "ext_configs.json.bak"
            if old.exists():
                self._migrate_legacy(old)
        if not self._base.exists():
            return []
        configs = []
        for d in sorted(self._base.iterdir()):
            cp = d / "config.json"
            if d.is_dir() and cp.exists():
                try:
                    raw = json.loads(cp.read_text(encoding="utf-8"))
                    configs.append(ExtConfig.from_dict(raw))
                except Exception as e:
                    logger.warning("扩展表配置解析失败 %s: %s", cp, e)
        return configs

    def get(self, config_id: str) -> ExtConfig | None:
        cp = self._config_path(config_id)
        if not cp.exists():
            return None
        try:
            raw = json.loads(cp.read_text(encoding="utf-8"))
            return ExtConfig.from_dict(raw)
        except Exception:
            return None

    def upsert(self, config: ExtConfig) -> None:
        config.updated_at = datetime.now().isoformat()
        cp = self._config_path(config.id)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def delete(self, config_id: str) -> bool:
        import shutil
        cp = self._config_path(config_id)
        if not cp.exists():
            return False
        shutil.rmtree(cp.parent, ignore_errors=True)
        return True

    def _migrate_legacy(self, old_path: Path) -> None:
        """一次性迁移旧版 ext_configs.json 到独立目录结构。"""
        try:
            raw = json.loads(old_path.read_text(encoding="utf-8"))
            configs = [ExtConfig.from_dict(d) for d in raw]
            for c in configs:
                cp = self._config_path(c.id)
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(
                    json.dumps(c.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            # 迁移完成后重命名旧文件作为备份
            backup = old_path.with_suffix(".json.bak")
            old_path.rename(backup)
            logger.info("ext_configs.json 已迁移至 ext/ (备份: %s)", backup.name)
        except Exception as e:
            logger.warning("ext_configs 迁移失败: %s", e)


# ---------------------------------------------------------------------------
# CSV / Excel 解析 → Parquet 写入
# ---------------------------------------------------------------------------

_POLARS_DTYPE_MAP = {
    "string": pl.Utf8,
    "int": pl.Int64,
    "float": pl.Float64,
    "bool": pl.Boolean,
}

_POLARS_TYPE_MAP = {
    "Int64": "int", "Int32": "int", "Int16": "int", "Int8": "int",
    "UInt64": "int", "UInt32": "int", "UInt16": "int", "UInt8": "int",
    "Float64": "float", "Float32": "float",
    "Boolean": "bool",
    "Utf8": "string", "String": "string",
    "Date": "string", "Datetime": "string", "Duration": "string",
    "Categorical": "string",
}

_CODE_PAT = re.compile(r"^\d{6}$")
_SYMBOL_PAT = re.compile(r"^\d{6}\.[A-Z]{2}$")


def infer_fields_from_df(df: pl.DataFrame) -> list[dict]:
    """从 DataFrame 推断扩展字段定义。"""
    fields = []
    for col_name in df.columns:
        pl_type = df[col_name].dtype
        dtype = _POLARS_TYPE_MAP.get(str(pl_type.base_type()), "string")
        fields.append({"name": col_name, "dtype": dtype, "label": col_name})
    return fields


def detect_symbol_candidates(df: pl.DataFrame) -> tuple[list[str], list[str]]:
    """识别 symbol/code 候选列。"""
    symbol_candidates: list[str] = []
    code_candidates: list[str] = []

    for col in df.columns:
        try:
            col_data = df[col].cast(pl.Utf8).drop_nulls()
        except Exception:
            continue
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

    return symbol_candidates, code_candidates


def build_code_lookup(data_dir: Path) -> dict[str, str]:
    """从 instruments 维表构建 code → symbol 映射。"""
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        return {}
    try:
        df = pl.read_parquet(path, columns=["code", "symbol"])
        return dict(zip(df["code"].to_list(), df["symbol"].to_list()))
    except Exception:
        return {}


def normalize_symbol(series: pl.Series, lookup: dict[str, str] | None = None) -> pl.Series:
    """将 symbol 列标准化为 代码.交易所 格式。

    优先使用 instruments 维表查找 code → symbol，确保 100% 准确。
    查不到时按规则兜底：6开头 → .SH，其余 → .SZ。
    """
    _lookup = lookup or {}

    def _fix_one(val: str) -> str:
        if not val:
            return val
        val = val.strip()
        # 已经是标准格式（含 .），直接返回
        if "." in val:
            return val
        # 纯6位数字代码 → 优先查维表
        if len(val) == 6 and val.isdigit():
            mapped = _lookup.get(val)
            if mapped:
                return mapped
            # 兜底规则
            if val.startswith(("6",)):
                return f"{val}.SH"
            else:
                return f"{val}.SZ"
        return val

    return series.map_elements(_fix_one, return_dtype=pl.Utf8)


def apply_config_mapping(df: pl.DataFrame, config: ExtConfig, data_dir: Path) -> pl.DataFrame:
    """根据 config 的 symbol_map / code_map 自动生成 symbol 和 code 列。"""
    sm = config.symbol_map or {}
    cm = config.code_map or {}

    if sm.get("type") == "mapped" and sm["col"] in df.columns:
        df = df.with_columns(df[sm["col"]].cast(pl.Utf8).alias("symbol"))

    if cm.get("type") == "mapped" and cm["col"] in df.columns:
        df = df.with_columns(df[cm["col"]].cast(pl.Utf8).alias("code"))

    if "symbol" not in df.columns and sm.get("type") == "computed":
        if sm.get("from") == "code" and "code" in df.columns:
            lookup = build_code_lookup(data_dir)
            df = df.with_columns(normalize_symbol(df["code"].cast(pl.Utf8), lookup).alias("symbol"))

    if "code" not in df.columns and cm.get("type") == "computed":
        if cm.get("from") == "symbol" and "symbol" in df.columns:
            df = df.with_columns(
                df["symbol"].cast(pl.Utf8).str.split(".").list.first().alias("code")
            )

    if "symbol" in df.columns and "code" not in df.columns:
        df = df.with_columns(
            df["symbol"].cast(pl.Utf8).str.split(".").list.first().alias("code")
        )
    elif "code" in df.columns and "symbol" not in df.columns:
        lookup = build_code_lookup(data_dir)
        df = df.with_columns(normalize_symbol(df["code"].cast(pl.Utf8), lookup).alias("symbol"))

    if "symbol" in df.columns:
        lookup = build_code_lookup(data_dir)
        df = df.with_columns(normalize_symbol(df["symbol"].cast(pl.Utf8), lookup))

    return df


def ensure_utf8_csv(file_path: Path) -> Path:
    """确保 CSV 文件以 UTF-8 编码可读，非 UTF-8（如 GBK/GB18030）则转换。

    国内行情软件（同花顺/东财/通达信）和 Windows 中文 Excel 导出的 CSV 多为
    GBK 系编码，Polars 的 read_csv 默认按 UTF-8 解析会抛 "invalid utf-8 sequence"。
    这里在交给 Polars 前做一次编码规范化。

    返回值：若已是 UTF-8 则返回原路径；否则在同目录写一个 *.utf8 文件并返回它
    （调用方用临时目录，随目录一起清理）。
    """
    raw = file_path.read_bytes()
    # BOM 处理：UTF-8-SIG 等带 BOM 文件直接交给 Polars（它认识 BOM）
    try:
        raw.decode("utf-8")
        return file_path  # 已是合法 UTF-8
    except UnicodeDecodeError:
        pass
    # 依次尝试常见中文编码，第一个能完整解码的即为命中
    for enc in ("gb18030", "gbk", "gb2312", "big5"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        out_path = file_path.with_suffix(file_path.suffix + ".utf8")
        out_path.write_text(text, encoding="utf-8")
        logger.info("CSV 编码转换 %s → %s (%s)", file_path.name, out_path.name, enc)
        return out_path
    # 都无法解码：返回原路径，让 Polars 抛出更精确的原始错误
    return file_path


def parse_upload_file(file_path: Path, symbol_col: str = "symbol", data_dir: Path | None = None) -> pl.DataFrame:
    """解析上传的 CSV / Excel 文件为 Polars DataFrame。"""
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        df = pl.read_csv(ensure_utf8_csv(file_path), infer_schema_length=10000)
    elif suffix in (".xlsx", ".xls"):
        df = pl.read_excel(file_path)
    else:
        raise ValueError(f"不支持的文件格式: {suffix}")

    if symbol_col not in df.columns:
        # 尝试模糊匹配
        candidates = [c for c in df.columns if c.lower() in ("symbol", "code", "代码", "标的")]
        if candidates:
            df = df.rename({candidates[0]: symbol_col})
        else:
            raise ValueError(f"未找到标的代码列 (symbol)，可选列: {df.columns}")

    # 确保 symbol 列为字符串并标准化
    lookup = build_code_lookup(data_dir) if data_dir else None
    df = df.with_columns(normalize_symbol(df[symbol_col].cast(pl.Utf8), lookup))
    return df


def cast_df_to_schema(df: pl.DataFrame, fields: list[ExtField]) -> pl.DataFrame:
    """按配置的字段类型转换 DataFrame 列类型。

    List → string 的处理: 上游接口常返回数组字段 (如 concepts: ["AI", "芯片"]),
    若声明为 string, 直接 cast 会抛 `cannot cast List type`。
    这里把列表元素先转字符串再以分号拼接, 与 _flatten_concept_rows 保持一致。
    """
    schema = df.schema
    for f in fields:
        if f.name not in df.columns:
            continue
        target = _POLARS_DTYPE_MAP.get(f.dtype, pl.Utf8)
        src = schema[f.name]
        if isinstance(src, pl.List) and target == pl.Utf8:
            df = df.with_columns(
                pl.col(f.name).cast(pl.List(pl.Utf8)).list.join(";").cast(target)
            )
        else:
            df = df.with_columns(pl.col(f.name).cast(target))
    return df


def _config_dir(config_id: str, data_dir: Path) -> Path:
    """返回扩展配置的根目录 data/ext_data/{config_id}/。"""
    return data_dir / "ext_data" / config_id


def write_ext_parquet(
    df: pl.DataFrame,
    config: ExtConfig,
    data_dir: Path,
    snapshot_date: date | None = None,
) -> int:
    """将 DataFrame 写入扩展数据 Parquet。

    目录结构:
      - snapshot:  data/ext_data/{id}/part.parquet（与 config.json 同级，覆盖写）
      - timeseries: data/ext_data/{id}/timeseries/date=xxx/part.parquet（按日分区）

    Returns:
        写入行数。
    """
    snap = snapshot_date or date.today()
    cfg_dir = _config_dir(config.id, data_dir)

    # 标准化 symbol 列: 用维表查找 → 准确匹配交易所
    if "symbol" in df.columns:
        lookup = build_code_lookup(data_dir)
        df = df.with_columns(normalize_symbol(df["symbol"], lookup))

    if config.mode == "snapshot":
        # 快照: 与 config.json 同级，直接覆盖
        cfg_dir.mkdir(parents=True, exist_ok=True)
        out_path = cfg_dir / "part.parquet"

        # 如果已有文件，合并去重后覆盖
        if out_path.exists():
            try:
                existing = pl.read_parquet(out_path)
                key = "symbol" if "symbol" in df.columns else df.columns[0]
                df = pl.concat([existing, df]).unique(subset=[key], keep="last")
            except Exception as e:
                # schema 不一致 (列不同) 时 concat 失败 → 直接用新 df 覆盖。
                # 记日志而非静默吞掉, 便于排查"数据结构错乱"类问题。
                logger.warning("扩展表 %s 合并去重失败, 将覆盖写入: %s", config.id, e)
    else:
        # 时序: timeseries/ 下按日期分区
        out_dir = cfg_dir / "timeseries" / f"date={snap}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "part.parquet"

        # 如果已有文件，合并去重
        if out_path.exists():
            try:
                existing = pl.read_parquet(out_path)
                key = "symbol" if "symbol" in df.columns else df.columns[0]
                df = pl.concat([existing, df]).unique(subset=[key], keep="last")
            except Exception as e:
                logger.warning("扩展表 %s 合并去重失败, 将覆盖写入: %s", config.id, e)

    df = cast_df_to_schema(df, config.fields)
    df.write_parquet(out_path)
    logger.info("扩展表写入: %s → %s (%d 行)", config.id, out_path, len(df))
    return len(df)


def delete_ext_parquet(config_id: str, data_dir: Path) -> None:
    """删除扩展数据源关联的所有 Parquet 数据（保留 config.json）。

    - snapshot: 删除 ext_data/{id}/part.parquet
    - timeseries: 删除 ext_data/{id}/timeseries/ 目录
    """
    cfg_dir = _config_dir(config_id, data_dir)
    # 删除快照文件
    snap = cfg_dir / "part.parquet"
    if snap.exists():
        snap.unlink()
    # 删除时序目录
    ts_dir = cfg_dir / "timeseries"
    if ts_dir.exists():
        import shutil
        shutil.rmtree(ts_dir, ignore_errors=True)


def fix_symbol_format(config: ExtConfig, data_dir: Path) -> int:
    """扫描该扩展配置的所有 Parquet 文件，将 symbol 列标准化为 代码.交易所 格式。

    - snapshot: 扫描 ext_data/{id}/part.parquet
    - timeseries: 扫描 ext_data/{id}/timeseries/date=xxx/part.parquet

    Returns:
        修复的文件数。
    """
    cfg_dir = _config_dir(config.id, data_dir)
    if not cfg_dir.exists():
        return 0

    # 收集需要扫描的 parquet 文件列表
    parquet_files: list[Path] = []
    if config.mode == "snapshot":
        p = cfg_dir / "part.parquet"
        if p.exists():
            parquet_files.append(p)
    else:
        ts_dir = cfg_dir / "timeseries"
        if ts_dir.exists():
            for part_dir in sorted(ts_dir.iterdir()):
                if not part_dir.is_dir() or not part_dir.name.startswith("date="):
                    continue
                p = part_dir / "part.parquet"
                if p.exists():
                    parquet_files.append(p)

    fixed = 0
    lookup = build_code_lookup(data_dir)
    for parquet_path in parquet_files:
        try:
            df = pl.read_parquet(parquet_path)
            if "symbol" not in df.columns:
                continue
            old = df["symbol"].to_list()
            df = df.with_columns(normalize_symbol(df["symbol"], lookup))
            new = df["symbol"].to_list()
            if old != new:
                df.write_parquet(parquet_path)
                fixed += 1
                logger.info("代码格式修复: %s/%s (%d 行)", config.id, parquet_path.parent.name, len(df))
        except Exception as e:
            logger.warning("代码格式修复跳过 %s: %s", parquet_path, e)

    return fixed


def rows_to_parquet(
    rows: list[dict],
    config: ExtConfig,
    data_dir: Path,
    snapshot_date: date | None = None,
) -> int:
    """将 JSON 行列表转为 DataFrame 写入 Parquet，复用 write_ext_parquet 的存储逻辑。

    Returns:
        写入行数。
    """
    df = pl.DataFrame(rows)
    df = apply_config_mapping(df, config, data_dir)
    if "symbol" in df.columns:
        df = df.with_columns(pl.col("symbol").cast(pl.Utf8))
    return write_ext_parquet(df, config, data_dir, snapshot_date=snapshot_date)
