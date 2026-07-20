"""stock-sdk 内置数据源 provider。

Original implementation by @forrany (PR #57), migrated to plugin architecture.
核心抓取/归一化逻辑保留原作者实现, 仅调整 import 路径与注册方式。

通过 bridge.mjs 调真实 stock-sdk 抓 A 股行情, 归一化到项目内部 schema。
方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
因此注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import polars as pl

from app.data_providers.base import AssetType
from app.data_providers.normalizer import normalize_adj_factors, normalize_daily
from app.plugins.stocksdk import bridge
from app.tickflow.rate_limits import chunked

logger = logging.getLogger(__name__)

# stock-sdk 支持的数据集(financial 不支持 → 不声明, 自动回退 tickflow)
_DATASETS = ("daily", "adj_factor", "minute", "realtime")

# 每次桥接调用的符号数。桥接内部按 concurrency 并发, 分批仅为进度反馈与超时控制。
_BATCH = 40
_MINUTE_CANONICAL = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]


@dataclass
class _StockSDKConfig:
    """轻量 config shim, 让 custom loader 的 list_sources/provider_has_dataset 能识别本 provider。"""

    name: str = "stocksdk"
    display_name: str = "stock-sdk（免费行情）"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _yyyymmdd(dt: datetime | None) -> str | None:
    return dt.strftime("%Y%m%d") if dt else None


class StockSDKProvider:
    """内置 stock-sdk 数据源。"""

    name = "stocksdk"
    builtin = True

    def __init__(self) -> None:
        self.config = _StockSDKConfig()

    def close(self) -> None:  # loader.load_all 会对每个 provider 调 close
        pass

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        logger.info("stock-sdk daily 拉取开始(%d symbols)", len(symbols))
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _BATCH)
        for i, chunk in enumerate(chunks):
            job = {
                "op": "daily",
                "symbols": chunk,
                "adjust": "none",
                "start": _yyyymmdd(start_time),
                "end": _yyyymmdd(end_time),
            }
            try:
                result = bridge.run_job(job, timeout=180)
            except bridge.StockSDKBridgeError as e:
                logger.warning("stock-sdk daily 拉取失败(%d symbols): %s", len(chunk), e)
                result = {"rows": {}}
            for sym, rows in (result.get("rows") or {}).items():
                if not rows:
                    continue
                df = normalize_daily(rows, default_symbol=sym, source=self.name)
                if not df.is_empty():
                    frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",  # noqa: ARG002
        on_chunk_done=None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _BATCH)
        for i, chunk in enumerate(chunks):
            job = {
                "op": "adj",
                "symbols": chunk,
                "start": _yyyymmdd(start_time),
                "end": _yyyymmdd(end_time),
            }
            try:
                result = bridge.run_job(job, timeout=240)
            except bridge.StockSDKBridgeError as e:
                logger.warning("stock-sdk adj 拉取失败(%d symbols): %s", len(chunk), e)
                result = {"rows": {}}
            flat: list[dict] = []
            for rows in (result.get("rows") or {}).values():
                flat.extend(rows or [])
            if flat:
                df = normalize_adj_factors(flat, source=self.name)
                if not df.is_empty():
                    frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",  # noqa: ARG002
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        period = "".join(ch for ch in str(freq) if ch.isdigit()) or "1"
        logger.info("stock-sdk minute 拉取开始(%d symbols, period=%s)", len(symbols), period)
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, _BATCH)
        for i, chunk in enumerate(chunks):
            job = {
                "op": "minute",
                "symbols": chunk,
                "period": period,
                "start": _yyyymmdd(start_time),
                "end": _yyyymmdd(end_time),
            }
            try:
                result = bridge.run_job(job, timeout=180)
            except bridge.StockSDKBridgeError as e:
                logger.warning("stock-sdk minute 拉取失败(%d symbols): %s", len(chunk), e)
                result = {"rows": {}}
            for sym, rows in (result.get("rows") or {}).items():
                df = self._minute_df(rows, sym)
                if not df.is_empty():
                    frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    @staticmethod
    def _minute_df(rows: list[dict], symbol: str) -> pl.DataFrame:
        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        # bridge 分钟行含 timestamp(ms, UTC 基准)。A 股分时按北京时间墙钟展示,
        # 故转 Asia/Shanghai 后去掉时区得到 naive 北京时间(如 09:35)。
        if "timestamp" in df.columns:
            df = df.with_columns(
                pl.from_epoch(pl.col("timestamp").cast(pl.Int64), time_unit="ms")
                .dt.replace_time_zone("UTC")
                .dt.convert_time_zone("Asia/Shanghai")
                .dt.replace_time_zone(None)
                .cast(pl.Datetime("us"))
                .alias("datetime")
            )
        elif "date" in df.columns:
            df = df.with_columns(
                pl.col("date").str.to_datetime("%Y-%m-%d %H:%M", strict=False).alias("datetime")
            )
        df = df.with_columns(pl.lit(symbol).alias("symbol"))
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
        keep = [c for c in _MINUTE_CANONICAL if c in df.columns]
        return df.select(keep) if "datetime" in keep else pl.DataFrame()

    # ---- realtime (全市场快照) ----
    def get_realtime(self) -> list[dict]:
        logger.info("stock-sdk realtime 拉取开始(全市场快照)")
        try:
            result = bridge.run_job({"op": "realtime"}, timeout=120)
        except bridge.StockSDKBridgeError as e:
            logger.warning("stock-sdk realtime 拉取失败: %s", e)
            return []
        return result.get("rows") or []

    # ---- instruments (标的维表) ----
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """返回 tickflow Instrument 形状的行(symbol/name/code/exchange/region/type + ext),

        供 instrument_sync._flatten_instruments 复用同一 flatten 路径, 列结构与 tickflow 一致。
        当前覆盖 A 股股票。
        """
        if asset_type != "stock":
            return []
        try:
            result = bridge.run_job({"op": "instruments"}, timeout=120)
        except bridge.StockSDKBridgeError as e:
            logger.warning("stock-sdk instruments 拉取失败: %s", e)
            return []
        return result.get("rows") or []

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        symbols = symbols or ["600519.SH"]
        if dataset == "daily":
            df = self.get_daily(symbols, None, None)
            return _preview("daily", df)
        if dataset == "adj_factor":
            df = self.get_adj_factors(symbols, None, None)
            return _preview("adj_factor", df)
        if dataset == "minute":
            df = self.get_minute(symbols, None, None)
            return _preview("minute", df)
        if dataset == "realtime":
            rows = self.get_realtime()
            head = rows[:5]
            return {
                "provider": self.name,
                "dataset": "realtime",
                "rows": len(rows),
                "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        raise ValueError(f"stock-sdk 不支持数据集: {dataset}")


def _preview(dataset: str, df: pl.DataFrame) -> dict:
    return {
        "provider": "stocksdk",
        "dataset": dataset,
        "rows": df.height,
        "columns": df.columns,
        "preview": df.head(5).to_dicts() if not df.is_empty() else [],
    }
