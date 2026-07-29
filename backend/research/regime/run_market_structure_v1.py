"""生成 2024-09-24 以来的结构牛市/结构熊市逐日标签和连续区间。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from collections import Counter
from datetime import date

import polars as pl

from research.paths import DATA_DIR, REGIME_ARTIFACTS_DIR
from research.regime.market_structure import (
    MarketStructureConfig,
    build_market_structure_features,
    classify_market_structure,
    market_structure_segments,
)


RESEARCH_START = date(2024, 9, 24)
OUTPUT = REGIME_ARTIFACTS_DIR / "market_structure_v1.json"
CACHE = DATA_DIR / ".regime_cache" / "market_structure_v1.parquet"


def _strict_json_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _strict_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strict_json_value(item) for item in value]
    return value


def _source_manifest(source_files: list[str], daily: pl.LazyFrame) -> dict:
    stats = daily.select(
        pl.len().alias("rows"),
        pl.col("symbol").n_unique().alias("symbols"),
        pl.col("date").min().alias("min_date"),
        pl.col("date").max().alias("max_date"),
        pl.struct(["symbol", "date"]).n_unique().alias("unique_keys"),
    ).collect().to_dicts()[0]
    digest = hashlib.sha256()
    for path in source_files:
        digest.update(path.encode("utf-8"))
    return {
        **stats,
        "files": len(source_files),
        "file_list_sha256": digest.hexdigest(),
        "source": "data/kline_daily_enriched/**/*.parquet",
    }


def main() -> None:
    config = MarketStructureConfig()
    source_files = sorted(
        str(path)
        for path in (DATA_DIR / "kline_daily_enriched").glob("**/*.parquet")
    )
    if not source_files:
        raise SystemExit("没有 kline_daily_enriched 数据")
    daily = (
        pl.scan_parquet(source_files, hive_partitioning=True)
        .select("symbol", "date", "close")
    )
    manifest = _source_manifest(source_files, daily)
    if int(manifest["rows"]) != int(manifest["unique_keys"]):
        raise SystemExit("行情存在重复 symbol/date，拒绝生成结构标签")

    features = build_market_structure_features(daily, config)
    full_labels = classify_market_structure(features, config)
    labels = full_labels.filter(pl.col("date") >= RESEARCH_START)
    if labels.is_empty():
        raise SystemExit("研究起点之后没有可用结构标签")
    segments = market_structure_segments(labels)
    counts = Counter(labels["regime"].to_list())
    latest = labels.sort("date").tail(1).to_dicts()[0]

    payload = {
        "version": "market_structure_v1",
        "evidence_status": "descriptive_regime_labels_not_strategy_validation",
        "research_start": RESEARCH_START,
        "config": config.to_dict(),
        "protocol_hash": config.protocol_hash(),
        "data_manifest": manifest,
        "definition": {
            "macro_context": (
                "2024-09-24 仅作为用户指定的大级别牛市研究起点；"
                "结构标签不使用这一先验直接决定每日状态。"
            ),
            "structural_bull": (
                "前一交易日 breadth(MA20)>=55%、breadth(MA60)>=50%、"
                "20日等权复合收益>=0，连续2日确认。"
            ),
            "structural_bear": (
                "前一交易日 breadth(MA20)<=45%，或 breadth(MA60)<=40%"
                "且20日等权复合收益<=-3%，连续2日确认。"
            ),
            "middle_band": "未触发相反状态时保持原状态，避免频繁切换。",
            "leakage_guard": "交易日 t 的标签只读取 t-1 或更早收盘数据。",
            "warmup_policy": (
                "MA/收益特征使用研究起点之前的本地行情暖机；"
                "产物只输出 2024-09-24 及之后标签。"
            ),
            "equal_weight_method": "先算逐股票日收益，再做截面均值并复合；不平均股票价格水平。",
        },
        "summary": {
            "trading_days": labels.height,
            "regime_days": dict(sorted(counts.items())),
            "state_changes": int(labels["state_change"].sum()),
            "latest_date": latest["date"],
            "latest_regime": latest["regime"],
            "latest_source_date": latest["source_date"],
            "warmup_note": (
                f"特征从 {manifest['min_date']} 起计算，研究标签从 "
                f"{RESEARCH_START.isoformat()} 起输出；交易日 t 仍只使用 t-1 或更早数据。"
            ),
        },
        "strategy_policy_hypotheses": {
            "structural_bull": ["trend_breakout", "bullish_alignment"],
            "structural_bear": ["cash", "pullback_to_support"],
            "warning": (
                "这里只预注册下一阶段对照，不代表收益已验证。历史证据不支持直接假设"
                " pullback 熊腿有效，必须与 bear=cash 同次比较。"
            ),
        },
        "segments": segments,
        "daily": labels.to_dicts(),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(
            _strict_json_value(payload),
            ensure_ascii=False,
            indent=2,
            default=str,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    cache = labels.select(
        "date",
        "source_date",
        "regime",
        "regime_code",
        "state_change",
        "decision_reason",
        "breadth_short",
        "breadth_long",
        "ew_return_20d",
        "structure_score",
    ).with_columns(pl.lit(config.protocol_hash()).alias("protocol_hash"))
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    cache_temp = CACHE.with_name(f".{CACHE.name}.{uuid.uuid4().hex}.tmp")
    try:
        cache.write_parquet(cache_temp)
        os.replace(cache_temp, CACHE)
    finally:
        if cache_temp.exists():
            cache_temp.unlink()
    print(
        f"[market-structure] {labels.height} days | changes={payload['summary']['state_changes']} "
        f"| latest={latest['date']} {latest['regime']} | {OUTPUT} | cache={CACHE}"
    )


if __name__ == "__main__":
    main()
