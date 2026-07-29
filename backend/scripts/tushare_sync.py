r"""安全、可断点续跑的 Tushare 日线增量同步。

默认行为只补缺失交易日，不删除也不重写已有股票日线。新分区会先在同目录写入
临时文件，通过 schema、日期、唯一键和 OHLC 合法性检查后再用 ``os.replace``
原子落盘。

从 ``backend/`` 运行：

    .\.venv\Scripts\python.exe -m scripts.tushare_sync \
        --start 20240924 --end 20260728

认证仅从 ``--ts-token`` 或 ``TUSHARE_TOKEN`` 读取，token 不会写入日志或产物。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

import polars as pl

from app.config import settings
from research.paths import ARCHIVE_ARTIFACTS_DIR, DATA_DIR


DEFAULT_RESEARCH_START = "20240924"
DEFAULT_WARMUP_START = "20240401"
DEFAULT_INDEX_SYMBOLS = (
    "000001.SH",  # 上证综指
    "399001.SZ",  # 深证成指
    "000300.SH",  # 沪深300
    "000905.SH",  # 中证500
    "000852.SH",  # 中证1000
    "399006.SZ",  # 创业板指
    "000688.SH",  # 科创50
)
_DAILY_COLUMNS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)


def _parse_api_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"invalid YYYYMMDD date: {value}") from exc


def _api_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _partition_path(data_dir: Path, table: str, trading_day: date) -> Path:
    return data_dir / table / f"date={trading_day.isoformat()}" / "part.parquet"


def _atomic_write_parquet(frame: pl.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.write_parquet(temp)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def _atomic_write_json(payload: Mapping[str, Any], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str, allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def _frame_from_tushare(data: Mapping[str, Any] | None) -> pl.DataFrame:
    if not data:
        return pl.DataFrame()
    fields = data.get("fields")
    items = data.get("items")
    if not isinstance(fields, list) or not isinstance(items, list) or not fields:
        return pl.DataFrame()
    return pl.DataFrame(
        {
            str(name): pl.Series(
                str(name),
                [row[index] for row in items],
                strict=False,
            )
            for index, name in enumerate(fields)
        }
    )


class TushareError(RuntimeError):
    """Tushare request failed after bounded retries."""


@dataclass
class TushareClient:
    base_url: str
    token: str
    timeout_seconds: int = 30
    retries: int = 6
    throttle_seconds: float = 0.10
    session: Any | None = None

    def post(
        self,
        api_name: str,
        params: Mapping[str, Any],
        *,
        fields: str,
    ) -> Mapping[str, Any]:
        if not self.token:
            raise TushareError("TUSHARE_TOKEN is not configured")
        if self.session is None:
            import requests

            self.session = requests.Session()

        body = {
            "api_name": api_name,
            "token": self.token,
            "params": dict(params),
            "fields": fields,
        }
        last_error = "unknown error"
        for attempt in range(self.retries):
            try:
                response = self.session.post(
                    self.base_url,
                    json=body,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") == 0 and payload.get("data") is not None:
                    if self.throttle_seconds > 0:
                        time.sleep(self.throttle_seconds)
                    return payload["data"]
                last_error = str(payload.get("msg") or f"code={payload.get('code')}")
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            if attempt + 1 < self.retries:
                time.sleep(1.2 * (attempt + 1))
        raise TushareError(f"{api_name} failed: {last_error}")


def _normalise_daily(data: Mapping[str, Any], trading_day: date) -> pl.DataFrame:
    frame = _frame_from_tushare(data)
    required = {"ts_code", "open", "high", "low", "close", "vol", "amount"}
    if frame.is_empty() or not required <= set(frame.columns):
        raise ValueError(f"daily response missing columns: {sorted(required - set(frame.columns))}")
    frame = (
        frame.rename({"ts_code": "symbol", "vol": "volume"})
        .with_columns(
            pl.lit(trading_day).cast(pl.Date).alias("date"),
            (pl.col("volume").cast(pl.Float64, strict=False) * 100.0).alias("volume"),
            (pl.col("amount").cast(pl.Float64, strict=False) * 1000.0).alias("amount"),
            *[
                pl.col(column).cast(pl.Float64, strict=False).alias(column)
                for column in ("open", "high", "low", "close")
            ],
        )
        .select(_DAILY_COLUMNS)
        .drop_nulls(["symbol", "date", "open", "high", "low", "close"])
        .unique(subset=["symbol", "date"], keep="last")
        .sort("symbol")
    )
    _validate_daily_partition(frame, trading_day)
    return frame


def _validate_daily_partition(frame: pl.DataFrame, trading_day: date) -> None:
    missing = set(_DAILY_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"daily partition missing columns: {sorted(missing)}")
    if frame.is_empty():
        raise ValueError("daily partition is empty")
    dates = frame["date"].cast(pl.Date, strict=False).unique().to_list()
    if dates != [trading_day]:
        raise ValueError(f"daily partition date mismatch: {dates} != {trading_day}")
    if frame.select(pl.struct(["symbol", "date"]).n_unique()).item() != frame.height:
        raise ValueError("daily partition contains duplicate symbol/date keys")
    invalid = frame.filter(
        ~(
            pl.col("open").is_finite()
            & pl.col("high").is_finite()
            & pl.col("low").is_finite()
            & pl.col("close").is_finite()
            & (pl.col("open") > 0)
            & (pl.col("high") >= pl.max_horizontal("open", "close"))
            & (pl.col("low") <= pl.min_horizontal("open", "close"))
            & (pl.col("low") > 0)
            & (pl.col("volume").fill_null(0) >= 0)
            & (pl.col("amount").fill_null(0) >= 0)
        )
    )
    if not invalid.is_empty():
        raise ValueError(f"daily partition contains {invalid.height} invalid OHLCV rows")


def _existing_daily_is_valid(path: Path, trading_day: date) -> bool:
    if not path.exists():
        return False
    try:
        _validate_daily_partition(pl.read_parquet(path), trading_day)
        return True
    except Exception:
        return False


def _normalise_adj_factor(data: Mapping[str, Any], trading_day: date) -> pl.DataFrame:
    frame = _frame_from_tushare(data)
    required = {"ts_code", "adj_factor"}
    if frame.is_empty() or not required <= set(frame.columns):
        raise ValueError(
            f"adj_factor response missing columns: {sorted(required - set(frame.columns))}"
        )
    frame = (
        frame.rename({"ts_code": "symbol"})
        .with_columns(
            pl.lit(trading_day).cast(pl.Date).alias("trade_date"),
            pl.col("adj_factor").cast(pl.Float64, strict=False),
        )
        .select("symbol", "trade_date", "adj_factor")
        .filter(
            pl.col("symbol").is_not_null()
            & pl.col("adj_factor").is_finite()
            & (pl.col("adj_factor") > 0)
        )
        .unique(subset=["symbol", "trade_date"], keep="last")
        .sort("symbol")
    )
    if frame.is_empty():
        raise ValueError("adj_factor response has no valid rows")
    return frame


def _normalise_daily_basic(data: Mapping[str, Any], trading_day: date) -> pl.DataFrame:
    frame = _frame_from_tushare(data)
    if frame.is_empty() or "ts_code" not in frame.columns:
        raise ValueError("daily_basic response is empty or missing ts_code")
    numeric = [
        column
        for column in (
            "turnover_rate",
            "turnover_rate_f",
            "volume_ratio",
            "total_share",
            "float_share",
            "free_share",
            "total_mv",
            "circ_mv",
        )
        if column in frame.columns
    ]
    frame = (
        frame.rename({"ts_code": "symbol"})
        .with_columns(
            pl.lit(trading_day).cast(pl.Date).alias("trade_date"),
            *[pl.col(column).cast(pl.Float64, strict=False) for column in numeric],
        )
        .unique(subset=["symbol", "trade_date"], keep="last")
        .sort("symbol")
    )
    return frame


def _normalise_index_daily(data: Mapping[str, Any], symbol: str) -> pl.DataFrame:
    frame = _frame_from_tushare(data)
    required = {"trade_date", "open", "high", "low", "close", "vol", "amount"}
    if frame.is_empty() or not required <= set(frame.columns):
        raise ValueError(
            f"index_daily response missing columns: {sorted(required - set(frame.columns))}"
        )
    if "ts_code" not in frame.columns:
        frame = frame.with_columns(pl.lit(symbol).alias("ts_code"))
    else:
        frame = frame.filter(pl.col("ts_code").cast(pl.Utf8) == symbol)
        if frame.is_empty():
            raise ValueError(
                f"index_daily response contains no rows for requested symbol {symbol}"
            )
    return (
        frame.rename({"ts_code": "symbol", "trade_date": "date", "vol": "volume"})
        .with_columns(
            pl.col("date").cast(pl.Utf8).str.to_date("%Y%m%d", strict=False),
            *[
                pl.col(column).cast(pl.Float64, strict=False).alias(column)
                for column in ("open", "high", "low", "close", "volume", "amount")
            ],
        )
        .select(_DAILY_COLUMNS)
        .drop_nulls(["symbol", "date", "open", "high", "low", "close"])
        .unique(subset=["symbol", "date"], keep="last")
        .sort(["date", "symbol"])
    )


def _contiguous_gap_predecessors(
    open_days: list[date],
    target_days: set[date],
) -> set[date]:
    """Return the immediate open day before every missing-day run."""
    predecessors: set[date] = set()
    for index, trading_day in enumerate(open_days):
        if trading_day not in target_days:
            continue
        if index > 0 and (index == 0 or open_days[index - 1] not in target_days):
            predecessors.add(open_days[index - 1])
    return predecessors


def _merge_new_ex_factors(
    existing_path: Path,
    cumulative_rows: Iterable[pl.DataFrame],
    target_days: set[date],
) -> int:
    cumulative = pl.concat(list(cumulative_rows), how="diagonal_relaxed")
    cumulative = cumulative.sort(["symbol", "trade_date"]).with_columns(
        (
            pl.col("adj_factor")
            / pl.col("adj_factor").shift(1).over("symbol")
        ).alias("ex_factor")
    )
    new = (
        cumulative.filter(pl.col("trade_date").is_in(sorted(target_days)))
        .with_columns(pl.col("ex_factor").fill_null(1.0))
        .select("symbol", "trade_date", "ex_factor")
    )
    if new.is_empty():
        return 0
    if existing_path.exists():
        existing = pl.read_parquet(existing_path)
        merged = pl.concat([existing, new], how="diagonal_relaxed").unique(
            subset=["symbol", "trade_date"],
            keep="last",
        )
    else:
        merged = new
    merged = merged.sort(["symbol", "trade_date"])
    _atomic_write_parquet(merged, existing_path)
    return new.height


def _merge_index_rows(
    data_dir: Path,
    frame: pl.DataFrame,
    *,
    refresh_existing: bool = False,
) -> int:
    written = 0
    for trading_day in frame["date"].unique().sort().to_list():
        incoming = frame.filter(pl.col("date") == trading_day)
        target = _partition_path(data_dir, "kline_index_daily", trading_day)
        if target.exists():
            current = pl.read_parquet(target)
            if not refresh_existing:
                existing_symbols = current["symbol"].cast(pl.Utf8).unique().to_list()
                incoming = incoming.filter(
                    ~pl.col("symbol").cast(pl.Utf8).is_in(existing_symbols)
                )
                if incoming.is_empty():
                    continue
            merged = pl.concat([current, incoming], how="diagonal_relaxed").unique(
                subset=["symbol", "date"],
                keep="last",
            )
        else:
            merged = incoming
        merged = merged.sort("symbol")
        _validate_daily_partition(merged, trading_day)
        _atomic_write_parquet(merged, target)
        written += incoming.height
    return written


def _symbols_sha256(symbols: Iterable[str]) -> str:
    payload = "\n".join(sorted(set(symbols))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sync_tushare(
    *,
    client: TushareClient,
    data_dir: Path,
    start: date,
    end: date,
    index_start: date,
    index_symbols: tuple[str, ...] = DEFAULT_INDEX_SYMBOLS,
    refresh_existing: bool = False,
    include_daily_basic: bool = True,
    run_enrichment: bool = True,
    workers: int = 1,
) -> dict[str, Any]:
    """Synchronize missing Tushare partitions without deleting old market data."""
    if start > end:
        raise ValueError("start must not be after end")
    if workers <= 0:
        raise ValueError("workers must be positive")
    if workers > 1 and not isinstance(client, TushareClient):
        raise TypeError("parallel sync requires a TushareClient")
    calendar_start = min(start, index_start) - timedelta(days=14)
    calendar_data = client.post(
        "trade_cal",
        {
            "exchange": "SSE",
            "start_date": _api_date(calendar_start),
            "end_date": _api_date(end),
            "is_open": "1",
        },
        fields="cal_date",
    )
    calendar = _frame_from_tushare(calendar_data)
    if calendar.is_empty() or "cal_date" not in calendar.columns:
        raise TushareError("trade_cal returned no open days")
    open_days = sorted(
        {
            _parse_api_date(str(value))
            for value in calendar["cal_date"].drop_nulls().to_list()
        }
    )
    target_open_days = [day for day in open_days if start <= day <= end]
    if not target_open_days:
        raise TushareError("trade_cal contains no target open days")

    missing_daily_days = {
        day
        for day in target_open_days
        if refresh_existing
        or not _existing_daily_is_valid(
            _partition_path(data_dir, "kline_daily", day),
            day,
        )
    }
    existing_adj_dates: set[date] = set()
    existing_adj_path = data_dir / "adj_factor" / "all.parquet"
    if existing_adj_path.exists() and not refresh_existing:
        try:
            existing_adj_dates = set(
                pl.read_parquet(existing_adj_path, columns=["trade_date"])
                ["trade_date"]
                .cast(pl.Date, strict=False)
                .drop_nulls()
                .unique()
                .to_list()
            )
        except Exception:
            existing_adj_dates = set()
    missing_adj_days = {
        day
        for day in target_open_days
        if refresh_existing or day not in existing_adj_dates
    }
    factor_days = set(missing_adj_days)
    factor_days.update(_contiguous_gap_predecessors(open_days, missing_adj_days))

    daily_basic_dir = data_dir / "tushare_daily_basic"
    missing_basic_days = {
        day
        for day in target_open_days
        if include_daily_basic
        and (
            refresh_existing
            or not (
                daily_basic_dir
                / f"date={day.isoformat()}"
                / "part.parquet"
            ).exists()
        )
    }
    work_days = missing_daily_days | factor_days | missing_basic_days

    summary: dict[str, Any] = {
        "status": "running",
        "source": "Tushare HTTP API",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "index_start": index_start.isoformat(),
        "refresh_existing": refresh_existing,
        "workers": workers,
        "existing_daily_partitions_skipped": len(target_open_days) - len(missing_daily_days),
        "target_open_days": len(target_open_days),
        "daily_days_written": 0,
        "daily_rows_written": 0,
        "daily_failures": [],
        "adj_factor_rows_merged": 0,
        "adj_factor_failures": [],
        "daily_basic_days_written": 0,
        "daily_basic_failures": [],
        "index_symbols": list(index_symbols),
        "index_rows_merged": 0,
        "failures": [],
        "pipeline_rows_written": 0,
    }

    def fetch_day(
        trading_day: date,
        request_client: TushareClient,
    ) -> tuple[date, Mapping[str, Any] | None, Mapping[str, Any] | None, Mapping[str, Any] | None, str | None]:
        needs_daily = trading_day in missing_daily_days
        needs_adj = trading_day in factor_days
        needs_basic = trading_day in missing_basic_days
        api_day = _api_date(trading_day)
        daily_data: Mapping[str, Any] | None = None
        adj_data: Mapping[str, Any] | None = None
        basic_data: Mapping[str, Any] | None = None
        basic_error: str | None = None
        if needs_daily:
            daily_data = request_client.post(
                "daily",
                {"trade_date": api_day},
                fields="ts_code,open,high,low,close,vol,amount",
            )

        if needs_adj:
            adj_data = request_client.post(
                "adj_factor",
                {"trade_date": api_day},
                fields="ts_code,adj_factor",
            )

        if needs_basic:
            try:
                basic_data = request_client.post(
                    "daily_basic",
                    {"trade_date": api_day},
                    fields=(
                        "ts_code,trade_date,turnover_rate,turnover_rate_f,"
                        "volume_ratio,total_share,float_share,free_share,total_mv,circ_mv"
                    ),
                )
            except TushareError as exc:
                basic_error = str(exc)
        return trading_day, daily_data, adj_data, basic_data, basic_error

    ordered_work_days = sorted(work_days)
    if workers == 1:
        fetched_days = (fetch_day(day, client) for day in ordered_work_days)
    else:
        thread_state = threading.local()

        def fetch_parallel(trading_day: date):
            request_client = getattr(thread_state, "client", None)
            if request_client is None:
                request_client = TushareClient(
                    base_url=client.base_url,
                    token=client.token,
                    timeout_seconds=client.timeout_seconds,
                    retries=client.retries,
                    throttle_seconds=client.throttle_seconds,
                )
                thread_state.client = request_client
            return fetch_day(trading_day, request_client)

        executor = ThreadPoolExecutor(max_workers=workers)
        fetched_days = executor.map(fetch_parallel, ordered_work_days)

    cumulative_adj: list[pl.DataFrame] = []
    try:
        for trading_day, daily_data, adj_data, basic_data, basic_error in fetched_days:
            if daily_data is not None:
                try:
                    daily = _normalise_daily(daily_data, trading_day)
                except ValueError as exc:
                    daily = None
                    summary["daily_failures"].append(
                        {"date": trading_day.isoformat(), "error": str(exc)}
                    )
            else:
                daily = None
            if adj_data is not None:
                try:
                    cumulative_adj.append(
                        _normalise_adj_factor(adj_data, trading_day)
                    )
                except ValueError as exc:
                    summary["adj_factor_failures"].append(
                        {"date": trading_day.isoformat(), "error": str(exc)}
                    )

            if daily is not None:
                target = _partition_path(data_dir, "kline_daily", trading_day)
                _atomic_write_parquet(daily, target)
                summary["daily_days_written"] += 1
                summary["daily_rows_written"] += daily.height

            if trading_day in missing_basic_days:
                basic_target = (
                    daily_basic_dir
                    / f"date={trading_day.isoformat()}"
                    / "part.parquet"
                )
                try:
                    if basic_error is not None:
                        raise TushareError(basic_error)
                    if basic_data is None:
                        raise ValueError("daily_basic returned no data")
                    basic = _normalise_daily_basic(basic_data, trading_day)
                    _atomic_write_parquet(basic, basic_target)
                    summary["daily_basic_days_written"] += 1
                except (TushareError, ValueError) as exc:
                    summary["daily_basic_failures"].append(
                        {"date": trading_day.isoformat(), "error": str(exc)}
                    )
    finally:
        if workers > 1:
            executor.shutdown(wait=True, cancel_futures=True)

    if missing_adj_days and cumulative_adj:
        summary["adj_factor_rows_merged"] = _merge_new_ex_factors(
            existing_adj_path,
            cumulative_adj,
            missing_adj_days,
        )

    for symbol in index_symbols:
        try:
            index_data = client.post(
                "index_daily",
                {
                    "ts_code": symbol,
                    "start_date": _api_date(index_start),
                    "end_date": _api_date(end),
                },
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
            )
            index_frame = _normalise_index_daily(index_data, symbol)
            if not index_frame.is_empty():
                summary["index_rows_merged"] += _merge_index_rows(
                    data_dir,
                    index_frame,
                    refresh_existing=refresh_existing,
                )
        except (TushareError, ValueError) as exc:
            summary["failures"].append(
                {"endpoint": "index_daily", "symbol": symbol, "error": str(exc)}
            )

    stock_basic_frames: list[pl.DataFrame] = []
    for listing_status in ("L", "D", "P"):
        try:
            data = client.post(
                "stock_basic",
                {"exchange": "", "list_status": listing_status},
                fields="ts_code,symbol,name,area,industry,market,list_date,delist_date,list_status",
            )
            frame = _frame_from_tushare(data)
            if not frame.is_empty():
                stock_basic_frames.append(frame)
        except TushareError as exc:
            summary["failures"].append(
                {"endpoint": "stock_basic", "list_status": listing_status, "error": str(exc)}
            )
    if stock_basic_frames:
        stock_basic = pl.concat(stock_basic_frames, how="diagonal_relaxed").unique(
            subset=["ts_code"],
            keep="last",
        )
        _atomic_write_parquet(
            stock_basic.sort("ts_code"),
            data_dir / "tushare_stock_basic" / "all.parquet",
        )
        summary["stock_basic_rows"] = stock_basic.height
        summary["stock_basic_symbols_sha256"] = _symbols_sha256(
            stock_basic["ts_code"].drop_nulls().cast(pl.Utf8).to_list()
        )

    if run_enrichment and (missing_daily_days or missing_adj_days):
        from app.indicators.pipeline import run_pipeline
        from app.tickflow.repository import DataStore, KlineRepository

        summary["pipeline_rows_written"] = int(run_pipeline(data_dir))
        KlineRepository(DataStore()).refresh_cache()

    has_failures = any(
        summary[key]
        for key in (
            "failures",
            "daily_failures",
            "adj_factor_failures",
            "daily_basic_failures",
        )
    )
    summary["status"] = "complete_with_failures" if has_failures else "complete"
    summary["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    summary["note"] = (
        "已有股票日线默认跳过；新分区校验后原子写入；未执行目录删除。"
        "空行情、复权因子或 daily_basic 失败不会覆盖股票日线，并会在对应失败列表中显式保留。"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="安全增量同步 Tushare 日线和结构行情所需数据")
    parser.add_argument("--start", default=DEFAULT_RESEARCH_START, help="股票研究起点 YYYYMMDD")
    parser.add_argument("--end", default=date.today().strftime("%Y%m%d"), help="终点 YYYYMMDD")
    parser.add_argument(
        "--index-start",
        default=DEFAULT_WARMUP_START,
        help="指数/结构信号暖机起点 YYYYMMDD",
    )
    parser.add_argument("--ts-token", default="", help="Tushare token；推荐使用环境变量")
    parser.add_argument(
        "--api-base",
        default="",
        help="Tushare HTTP API base；默认读取 TUSHARE_API_BASE",
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="显式刷新已有分区；默认绝不重拉已有有效日线",
    )
    parser.add_argument(
        "--skip-daily-basic",
        action="store_true",
        help="跳过需要更高积分权限的 daily_basic",
    )
    parser.add_argument(
        "--skip-pipeline",
        action="store_true",
        help="只同步原始数据，不重算 enriched",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并发请求数；落盘顺序仍按交易日确定，默认 1",
    )
    args = parser.parse_args()

    token = args.ts_token or os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        raise SystemExit("请通过 --ts-token 或 TUSHARE_TOKEN 配置 token")
    base_url = (
        args.api_base
        or os.environ.get("TUSHARE_API_BASE", "")
        or "http://api.tushare.pro"
    )
    result = sync_tushare(
        client=TushareClient(base_url=base_url, token=token),
        data_dir=Path(settings.data_dir),
        start=_parse_api_date(args.start),
        end=_parse_api_date(args.end),
        index_start=_parse_api_date(args.index_start),
        refresh_existing=bool(args.refresh_existing),
        include_daily_basic=not args.skip_daily_basic,
        run_enrichment=not args.skip_pipeline,
        workers=args.workers,
    )
    artifact_dir = ARCHIVE_ARTIFACTS_DIR / "data"
    artifact = artifact_dir / "tushare_sync_latest.json"
    _atomic_write_json(result, artifact)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False))
    print(f"manifest: {artifact}")


if __name__ == "__main__":
    main()
