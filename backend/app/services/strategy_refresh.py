"""统一重算并校验策略缓存。

盘后数据落盘后必须通过本服务刷新策略结果。只有缓存文件能够被重新读取,
且日期和本次全部策略一致,才算刷新成功;避免日 K 已到当天、策略仍停在前一天。
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict
from datetime import date
from typing import Any

from app.services import strategy_cache
from app.services.screener import ScreenerService
from app.strategy import config as strategy_config

logger = logging.getLogger(__name__)


class UnknownStrategiesError(ValueError):
    """调用方请求了策略引擎中不存在的策略。"""

    def __init__(self, strategy_ids: list[str]):
        self.strategy_ids = strategy_ids
        super().__init__(f"unknown strategies: {strategy_ids}")


def _json_safe(value: Any) -> Any:
    """递归清理 JSON 不支持的 NaN/Inf,同时不改动引擎原始结果。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def refresh_strategy_cache(
    repo,
    engine,
    *,
    as_of: date | str | None = None,
    asset_type: str = "stock",
    timeframe: str = "1d",
    strategy_ids: list[str] | None = None,
    screener_service: ScreenerService | None = None,
) -> dict[str, Any]:
    """运行指定范围内的全部策略,原子写缓存并回读校验。

    返回本次计算收据,其中 ``results`` 仅含本次策略,不混入同日旧缓存。
    没有交易日、没有可运行策略、写入未生效或回读不完整都会抛异常,让盘后
    Job 明确失败,而不是继续展示旧日期结果。
    """
    if engine is None:
        raise RuntimeError("策略引擎未初始化")

    svc = screener_service or ScreenerService(repo, asset_type=asset_type)
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)
    actual_date = as_of or svc.latest_date()
    if not actual_date:
        raise RuntimeError("没有可用于策略计算的最新交易日")

    if strategy_ids is not None:
        all_ids = [str(strategy_id) for strategy_id in strategy_ids]
        unknown = [strategy_id for strategy_id in all_ids if not engine.has(strategy_id)]
        if unknown:
            raise UnknownStrategiesError(unknown)
    else:
        all_ids = [
            str(meta["id"])
            for meta in engine.list_strategies()
            if asset_type in meta.get("asset_types", ["stock"])
            and timeframe in meta.get("timeframes", ["1d"])
        ]

    if not all_ids:
        raise RuntimeError(f"没有符合 {asset_type}/{timeframe} 的可运行策略")

    started = time.perf_counter()
    data_dir = repo.store.data_dir
    all_overrides = strategy_config.list_overrides(data_dir)
    params_map = {
        strategy_id: dict((all_overrides.get(strategy_id) or {}).get("params") or {})
        for strategy_id in all_ids
    }
    overrides_map = {
        strategy_id: all_overrides.get(strategy_id, {})
        for strategy_id in all_ids
    }
    context = svc.build_strategy_context(
        engine,
        actual_date,
        all_ids,
        timeframe=timeframe,
        params_map=params_map,
        overrides_map=overrides_map,
    )
    engine_results = engine.run_all(
        context,
        params_map=params_map,
        overrides_map=overrides_map,
        strategy_ids=all_ids,
    )

    results: dict[str, dict[str, Any]] = {}
    for strategy_id, result in engine_results.items():
        safe_result = _json_safe(asdict(result))
        results[str(strategy_id)] = {
            "total": result.total,
            "as_of": actual_date.isoformat(),
            "rows": safe_result.get("rows", []),
        }

    missing_current = [
        strategy_id for strategy_id in all_ids if strategy_id not in results
    ]
    if missing_current:
        raise RuntimeError(
            f"本次策略计算结果不完整,缺失策略: {missing_current}"
        )

    strategy_cache.write_cache(data_dir, actual_date.isoformat(), results)

    cached = strategy_cache.read_cache(data_dir)
    cached_results = (cached or {}).get("results") or {}
    missing = [strategy_id for strategy_id in all_ids if strategy_id not in cached_results]
    wrong_dates = [
        strategy_id
        for strategy_id in all_ids
        if str((cached_results.get(strategy_id) or {}).get("as_of")) != actual_date.isoformat()
    ]
    if (
        not cached
        or str(cached.get("as_of")) != actual_date.isoformat()
        or missing
        or wrong_dates
    ):
        raise RuntimeError(
            "策略缓存写入后校验失败"
            f"(期望日期={actual_date.isoformat()}, "
            f"实际日期={(cached or {}).get('as_of')}, "
            f"缺失策略={missing}, 日期异常策略={wrong_dates})"
        )

    matched_rows = sum(len(result.get("rows", [])) for result in results.values())
    logger.info(
        "策略缓存刷新并校验通过: as_of=%s, strategies=%d, matched=%d, elapsed=%.1fms",
        actual_date,
        len(all_ids),
        matched_rows,
        (time.perf_counter() - started) * 1000,
    )
    return {
        "as_of": actual_date.isoformat(),
        "strategy_count": len(all_ids),
        "matched_rows": matched_rows,
        "results": results,
    }
