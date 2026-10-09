"""Generic HTTP provider for custom market data sources."""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from app.config import settings
from app.data_providers.base import AssetType
from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.mapper import (
    apply_transforms,
    datetime_payload,
    extract_rows,
    map_rows,
)
from app.data_providers.normalizer import normalize_adj_factors, normalize_daily
from app.market_time import cn_now
from app.tickflow.rate_limits import chunked, sleep_between_batches

logger = logging.getLogger(__name__)

_REQUIRED = {
    "daily": {"symbol", "date", "open", "high", "low", "close", "volume", "amount"},
    "adj_factor": {"symbol", "trade_date", "ex_factor"},
    "realtime": {"symbol", "last_price", "prev_close", "open", "high", "low", "volume"},
    "minute": {"symbol", "datetime", "open", "high", "low", "close", "volume"},
    # full_minute (全量分钟) 与 minute 同形: 当日窗口批量拉取, 字段映射一致
    "full_minute": {"symbol", "datetime", "open", "high", "low", "close", "volume"},
    # minute/full_minute 的 amount(成交额)按契约可空 (docs/plugin-development.md:
    # 无法提供可靠成交额时应返回 null 不得伪造, 均价线降级显示 —), 不列为必填 —
    # 否则缺成交额的源整个被 validate 拒绝, 保存后源从列表消失
    # financial 字段由数据源决定, 只要求能映射出 symbol
    "financial": {"symbol"},
}

# 小数制下 change_pct 的物理上限: A股最大涨跌停 30% (+容差)。
# 中位数口径下小数制批次不可能超过该值, 百分制批次(典型中位数 0.5~3)必然超过。
# 仅对 change_pct 有效——amplitude/turnover_rate 的两种单位在数值区间上重叠
# (百分制 0.05 = 0.05% 与小数制 0.05 = 5%), 无物理依据可判。
_PCT_FRACTION_MAX = 0.31

_PCT_COLUMNS = ("change_pct", "amplitude", "turnover_rate")


def _normalize_pct_units(
    df: pl.DataFrame,
    pct_unit: str | None = None,
    transformed_cols: frozenset[str] = frozenset(),
) -> pl.DataFrame:
    """比例字段单位归一为契约小数制 (change_pct/amplitude/turnover_rate,
    0.0366 = 3.66%, CONTRIBUTING §3.1)。单位只认显式声明, 不靠数值猜:

      - pct_unit="percent"  → 三列无条件 /100 (声明即契约, 即使数值看着像小数制);
      - pct_unit="decimal"  → 原样透传 (即使数值看着像百分制也不动);
      - 未声明 → change_pct 保留截面中位数判定(涨跌停 30% 上限使其物理可判:
        样本 >= 5 用 |值| 中位数, 小样本退用最大值, 整批同除 100);
        amplitude/turnover_rate 置 None 交下游重算(enriched 管道按
        high/low/prev_close 与股本口径重算), 除非该列已被 transforms 显式
        处理过(视为用户已接管单位, 原样透传)。
    """
    dropped_undeclared = False
    for col in _PCT_COLUMNS:
        if col not in df.columns:
            continue
        df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False).alias(col))
        if pct_unit == "percent":
            df = df.with_columns((pl.col(col) / 100).alias(col))
        elif pct_unit == "decimal" or col in transformed_cols:
            continue
        elif col == "change_pct":
            vals = df[col].drop_nulls().abs()
            if vals.is_empty():
                continue
            stat = vals.median() if vals.len() >= 5 else vals.max()
            if stat > _PCT_FRACTION_MAX:
                df = df.with_columns((pl.col(col) / 100).alias(col))
        else:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
            dropped_undeclared = True
    if dropped_undeclared:
        logger.warning(
            "自定义源 realtime 未声明 pct_unit: amplitude/turnover_rate 的单位"
            "无法从数值判定, 已置 None 交由下游按股本/价格口径重算;"
            "请在 realtime 数据集配置中显式声明 pct_unit: percent 或 decimal"
        )
    return df


class GenericHTTPProvider:
    """HTTP-backed custom source. It only handles fetching and schema mapping."""

    def __init__(self, config: CustomSourceConfig) -> None:
        self.config = config
        self.name = config.name
        self._client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def validate(self) -> list[str]:
        errors: list[str] = []
        for dataset, cfg in self.config.datasets.items():
            if not cfg.url:
                errors.append(f"{dataset}: url is required")
            required = _REQUIRED.get(dataset)
            if required:
                mapped = set(cfg.field_map.values())
                missing = sorted(required - mapped)
                if missing:
                    errors.append(f"{dataset}: missing mapped fields: {', '.join(missing)}")
            if cfg.pct_unit is not None:
                if dataset != "realtime":
                    errors.append(f"{dataset}: pct_unit 仅用于 realtime 数据集")
                elif cfg.pct_unit not in ("percent", "decimal"):
                    errors.append(f"{dataset}: pct_unit 必须是 percent 或 decimal")
            if dataset != "realtime":
                request_params = [cfg.symbols_param, cfg.start_param, cfg.end_param]
                if dataset in {"minute", "full_minute"}:
                    request_params.extend(
                        name for name in (cfg.asset_type_param, cfg.freq_param) if name
                    )
                duplicates = sorted({
                    name for name in request_params if request_params.count(name) > 1
                })
                if duplicates:
                    errors.append(
                        f"{dataset}: duplicate request parameter names: "
                        f"{', '.join(duplicates)}"
                    )
        return errors

    def _request_rows_retry(
        self, cfg, symbols: list[str], *, start_time=None, end_time=None, retries: int = 1
    ) -> list[dict]:
        """单批请求 + 短退避重试。仍失败抛出, 由调用方决定隔离粒度 (#226)。"""
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return self._request_rows(
                    cfg, symbols=symbols, start_time=start_time, end_time=end_time
                )
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
        assert last is not None
        raise last

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
    ) -> pl.DataFrame:
        cfg = self._dataset("daily")
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        failed: list[str] = []
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            try:
                rows = self._request_rows_retry(
                    cfg, chunk, start_time=start_time, end_time=end_time
                )
            except Exception as e:  # noqa: BLE001
                # 单批失败只隔离该批 (#226): 之前任一批 502 会让整个 stage
                # 抛异常, 已成功批次的结果留在内存里全部丢弃
                failed.extend(chunk)
                logger.warning(
                    "custom daily: batch %d/%d failed (%d symbols), skipped: %s",
                    i + 1, len(chunks), len(chunk), e,
                )
                if on_chunk_done:
                    on_chunk_done(i + 1, len(chunks))
                continue
            df = self._mapped_frame(cfg, rows)
            df = normalize_daily(df, source=self.name)
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        if failed:
            logger.warning(
                "custom daily: %d/%d symbols missing due to batch failures: %s",
                len(failed), len(symbols), ", ".join(failed[:20]),
            )
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
    ) -> pl.DataFrame:
        cfg = self._dataset("adj_factor")
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        failed: list[str] = []
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            try:
                rows = self._request_rows_retry(
                    cfg, chunk, start_time=start_time, end_time=end_time
                )
            except Exception as e:  # noqa: BLE001
                failed.extend(chunk)
                logger.warning(
                    "custom adj_factor: batch %d/%d failed (%d symbols), skipped: %s",
                    i + 1, len(chunks), len(chunk), e,
                )
                if on_chunk_done:
                    on_chunk_done(i + 1, len(chunks))
                continue
            df = self._mapped_frame(cfg, rows)
            df = normalize_adj_factors(df, source=self.name)
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        if failed:
            logger.warning(
                "custom adj_factor: %d/%d symbols missing due to batch failures: %s",
                len(failed), len(symbols), ", ".join(failed[:20]),
            )
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_realtime(self) -> list[dict]:
        cfg = self._dataset("realtime")
        rows = self._request_rows(cfg)
        df = self._mapped_frame(cfg, rows)
        # 单位归一: 显式 pct_unit 声明优先; 未声明时 amplitude/turnover_rate
        # fail-closed 置 None(交下游重算), change_pct 保留截面判定
        df = _normalize_pct_units(
            df,
            pct_unit=cfg.pct_unit,
            transformed_cols=frozenset(cfg.transforms) & set(_PCT_COLUMNS),
        )
        if df.is_empty():
            return []
        return df.to_dicts()

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """拉取分钟 K。

        asset_type / freq 默认不传上游 (minute dataset URL 应返回 1m 数据)。
        在 dataset 配置中设置 asset_type_param / freq_param 后, 这两个参数会以
        配置的参数名注入请求 (GET → params, POST → body), 用于上游需区分
        stock/ETF/index 或固定频率的场景。
        """
        return self._fetch_minute_dataset(
            "minute", symbols, start_time, end_time, asset_type, freq, on_chunk_done,
        )

    def get_intraday_batch(
        self,
        symbols: list[str],
        count: int = 300,  # noqa: ARG002 — 与插件契约对齐, YAML 源按时间窗口取全天
        asset_type: AssetType = "stock",
    ) -> pl.DataFrame:
        """全量分钟修复轮: 按当日窗口批量拉取 full_minute 数据集 (chunked + rpm 限速)。

        与 get_minute 同形 (字段映射/归一一致), 区别仅在数据集名与窗口由调用方
        传当日值。稳态增量 (get_intraday_latest) YAML 声明式源不提供 — 服务自动
        降级为仅修复轮模式并放慢节奏。
        """
        # 当日窗口按北京时间墙钟 (naive, 与分钟K契约及 kline_sync.fetch_intraday_custom_batch
        # 回退路径同口径): datetime.now() 取服务器本地时区, UTC 主机上窗口整体早 8 小时
        end = cn_now().replace(tzinfo=None)
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        return self._fetch_minute_dataset(
            "full_minute", symbols, start, end, asset_type, "1m", None,
        )

    def _fetch_minute_dataset(
        self,
        ds_name: str,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        cfg = self._dataset(ds_name)
        override: dict[str, Any] = {}
        if cfg.asset_type_param:
            override[cfg.asset_type_param] = asset_type
        if cfg.freq_param:
            override[cfg.freq_param] = freq
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            rows = self._request_rows(
                cfg, symbols=chunk, start_time=start_time, end_time=end_time,
                override_params=override or None, override_body=override or None,
            )
            df = self._mapped_frame(cfg, rows)
            df = self._normalize_minute(df)
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """拉取财务数据。table 包含四张财务报表及 shares 股本表。

        custom 源用一个 'financial' dataset 配置覆盖全部财务表; 请求时把 table 作为参数传给上游,
        上游根据 table 返回对应数据。字段由数据源决定, 这里只确保有 symbol 列。
        """
        cfg = self._dataset("financial")
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            # 把 table 注入到请求参数 (上游据此区分财务表)
            extra_params = {**cfg.params, "table": table}
            extra_body = {**cfg.body, "table": table}
            if table == "shares":
                extra_params["latest"] = latest_only
                extra_body["latest"] = latest_only
            rows = self._request_rows(
                cfg, symbols=chunk,
                override_params=extra_params, override_body=extra_body,
            )
            df = self._mapped_frame(cfg, rows)
            if not df.is_empty():
                frames.append(df)
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal_relaxed")

    @classmethod
    def _normalize_minute(cls, df: pl.DataFrame) -> pl.DataFrame:
        """把映射后的 df 规范成 minute canonical 列。"""
        if df.is_empty():
            return df
        if "datetime" in df.columns and df.schema["datetime"] != pl.Datetime("us"):
            if df.schema["datetime"] == pl.Utf8:
                # 字符串 datetime 直接 cast 会整体置 null (polars 不做字符串解析);
                # 先解析再对齐微秒精度 (#225, 参照
                # kline_sync._enforce_minute_beijing_wallclock 的处理)。
                # Series 级立即解析: 表达式错误要到 collect 才抛, 无法按格式回退
                df = df.with_columns(cls._parse_datetime_series(df["datetime"]))
            df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
        keep = [c for c in ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount") if c in df.columns]
        return df.select(keep) if keep else pl.DataFrame()

    _DATETIME_STR_FORMATS = (
        None,  # 自动推断
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    )

    @classmethod
    def _parse_datetime_series(cls, s: pl.Series) -> pl.Series:
        """逐格式尝试解析字符串 datetime; 均失败返回全 null (宽松语义)。"""
        for fmt in cls._DATETIME_STR_FORMATS:
            try:
                return (
                    s.str.to_datetime(strict=False, format=fmt)
                    if fmt else s.str.to_datetime(strict=False)
                )
            except Exception:  # noqa: BLE001 — 该格式不适用, 换下一个
                continue
        return pl.Series("datetime", [None] * s.len(), dtype=pl.Datetime("us"))

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        cfg = self._dataset(dataset)
        test_symbols = symbols or ["000001.SZ"]
        end_time = datetime.now()
        start_time = end_time - timedelta(days=7)
        if dataset == "realtime":
            rows = self._request_rows(cfg)
        elif dataset in {"minute", "full_minute"}:
            override: dict[str, Any] = {}
            if cfg.asset_type_param:
                override[cfg.asset_type_param] = "stock"
            if cfg.freq_param:
                override[cfg.freq_param] = "1m"
            rows = self._request_rows(
                cfg,
                symbols=test_symbols,
                start_time=start_time,
                end_time=end_time,
                override_params=override or None,
                override_body=override or None,
            )
        elif dataset in {"daily", "adj_factor"}:
            rows = self._request_rows(
                cfg,
                symbols=test_symbols,
                start_time=start_time,
                end_time=end_time,
            )
        else:
            rows = self._request_rows(cfg, symbols=test_symbols)
        df = self._mapped_frame(cfg, rows)
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": len(rows),
            "columns": df.columns,
            "preview": df.head(5).to_dicts() if not df.is_empty() else [],
        }

    def _dataset(self, name: str) -> DatasetConfig:
        cfg = self.config.datasets.get(name)
        if not cfg:
            raise ValueError(f"Custom data source '{self.name}' does not configure dataset '{name}'")
        return cfg

    def _mapped_frame(self, cfg: DatasetConfig, rows: list[dict]) -> pl.DataFrame:
        df = map_rows(rows, cfg.field_map)
        return apply_transforms(df, cfg.transforms)

    def _request_rows(
        self,
        cfg: DatasetConfig,
        *,
        symbols: list[str] | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        override_params: dict[str, Any] | None = None,
        override_body: dict[str, Any] | None = None,
    ) -> list[dict]:
        headers, auth_params = self._auth_parts()
        params = dict(cfg.params)
        params.update(auth_params)
        if override_params:
            params.update(override_params)
        body = dict(cfg.body)
        if override_body:
            body.update(override_body)
        if symbols:
            body[cfg.symbols_param] = symbols
            params.setdefault(cfg.symbols_param, ",".join(symbols))
        start_value = datetime_payload(start_time)
        end_value = datetime_payload(end_time)
        if start_value:
            body[cfg.start_param] = start_value
            params.setdefault(cfg.start_param, start_value)
        if end_value:
            body[cfg.end_param] = end_value
            params.setdefault(cfg.end_param, end_value)

        method = cfg.method.upper()
        request_kwargs: dict[str, Any] = {"headers": headers, "timeout": cfg.timeout}
        if method == "GET":
            request_kwargs["params"] = params
        else:
            request_kwargs["params"] = auth_params
            request_kwargs["json"] = body
        resp = self._client.request(method, cfg.url, **request_kwargs)
        resp.raise_for_status()
        return extract_rows(resp.json(), cfg.response_path)

    def _auth_parts(self) -> tuple[dict[str, str], dict[str, str]]:
        auth = self.config.auth
        if auth.type == "none":
            return {}, {}
        token = _token_from_env(auth.token_env) if auth.token_env else None
        if not token:
            logger.warning("custom data source %s auth token is not set", self.name)
            return {}, {}
        if auth.type == "bearer":
            return {auth.header: f"Bearer {token}"}, {}
        if auth.type == "header":
            return {auth.header: token}, {}
        if auth.type == "query":
            return {}, {auth.param: token}
        return {}, {}


def _token_from_env(name: str | None) -> str | None:
    if not name:
        return None
    token = os.getenv(name)
    if token:
        return token
    candidates = [settings.data_dir.parent / ".env", Path.cwd() / ".env", Path.cwd().parent / ".env"]
    env_path = next((path for path in candidates if path.exists()), None)
    if env_path is None:
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        return None
    return None
