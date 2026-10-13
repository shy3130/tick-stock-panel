"""情绪评分口径 — 默认口径还原 + 配置归一化契约。

「默认口径必须与历史硬编码算法逐位一致」是这里的核心约束: 情绪分是看板与
复盘快照的对外数字, 改动计算模块不能悄悄挪动它。原算法已从
market_overview_builder 移除, 故本文件内联一份等价实现做对照。
"""
from __future__ import annotations

import math
import random

from app.services.emotion_score import (
    DEFAULT_CONFIG,
    DIMENSION_ORDER,
    compute,
    dimension_value,
    label_of,
    merge_config,
    public_config,
    score_of,
)


def _legacy_score(value: float, low: float, high: float) -> int:
    """改造前 market_overview_builder._score 的逐字复制。"""
    if high <= low:
        return 50
    if not math.isfinite(value):
        return 50
    return max(0, min(100, round((value - low) / (high - low) * 100)))


def _legacy_radar_and_score(m: dict) -> tuple[list[dict], int]:
    """改造前看板情绪雷达的逐字复制 (含 mainline 预计算那一版)。"""
    radar = [
        {"key": "index", "label": "指数", "value": _legacy_score(m["avg_index_pct"], -2.5, 2.5)},
        {"key": "profit", "label": "赚钱", "value": round(
            _legacy_score(m["up_pct"], 20, 80) * 0.45
            + _legacy_score(m["avg_pct"], -0.02, 0.02) * 0.25
            + _legacy_score(m["median_pct"], -0.02, 0.02) * 0.20
            + _legacy_score(m["strong_diff_pct"], -8, 8) * 0.10)},
        {"key": "money", "label": "量能", "value": round(
            _legacy_score(m["avg_vol_ratio"], 0.6, 1.8) * 0.70
            + _legacy_score(m["high_vol_pct"], 2, 12) * 0.30)},
        {"key": "speculation", "label": "投机", "value": round(
            _legacy_score(m["limit_up"], 5, 90) * 0.25
            + _legacy_score(m["seal_rate"], 30, 85) * 0.35
            + _legacy_score(m["max_boards"], 1, 8) * 0.25
            + _legacy_score(m["tier2_count"], 0, 30) * 0.15)},
        {"key": "resilience", "label": "抗跌", "value": 100 - round(
            _legacy_score(m["down_pct"], 20, 80) * 0.55
            + _legacy_score(m["strong_down_pct"], 1, 12) * 0.45)},
        {"key": "mainline", "label": "主线", "value": m["mainline_score"]},
    ]
    return radar, round(sum(r["value"] for r in radar) / len(radar))


def _sample(rng: random.Random) -> dict:
    m = {
        "avg_index_pct": rng.uniform(-6, 6), "up_pct": rng.uniform(0, 100),
        "avg_pct": rng.uniform(-0.05, 0.05), "median_pct": rng.uniform(-0.05, 0.05),
        "strong_diff_pct": rng.uniform(-20, 20), "avg_vol_ratio": rng.uniform(0.3, 3),
        "high_vol_pct": rng.uniform(0, 40), "limit_up": rng.randint(0, 200),
        "seal_rate": rng.uniform(0, 100), "max_boards": rng.randint(0, 12),
        "tier2_count": rng.randint(0, 60), "down_pct": rng.uniform(0, 100),
        "strong_down_pct": rng.uniform(0, 25), "mainline_avg": rng.uniform(-0.03, 0.08),
        "mainline_cover_pct": rng.uniform(0, 30),
    }
    m["mainline_score"] = round(
        _legacy_score(m["mainline_avg"], -0.005, 0.03) * 0.65
        + _legacy_score(m["mainline_cover_pct"], 1, 12) * 0.35)
    return m


def test_default_matches_legacy_bit_for_bit() -> None:
    """出厂口径与历史硬编码算法逐位一致 (随机 5000 组)。

    取整顺序在国内很敏感: 抗跌维度必须「先取整再取反」(100 - round(x)),
    写成 round(100 - x) 会在 x 的小数部分恰为 .5 时差 1 分。
    """
    rng = random.Random(7)
    for _ in range(5000):
        m = _sample(rng)
        legacy_radar, legacy_score = _legacy_radar_and_score(m)
        res = compute(m, None)
        assert res["radar"] == legacy_radar, f"雷达不一致: {m}"
        assert res["score"] == legacy_score, f"总分不一致: {m}"


def test_label_mapping_matches_legacy_over_full_range() -> None:
    """档位映射覆盖 0..100 全整数区间, 与历史阈值一致。"""
    def legacy_label(score: int) -> str:
        if score >= 70:
            return "强势"
        if score >= 55:
            return "偏暖"
        if score >= 45:
            return "震荡"
        if score >= 30:
            return "偏冷"
        return "冰点"

    th = DEFAULT_CONFIG["thresholds"]
    for score in range(101):
        assert label_of(score, th) == legacy_label(score), f"{score} 档位不一致"


def test_score_of_handles_non_finite() -> None:
    """非有限输入取中性 50, 不得抛异常打断看板计算。"""
    for bad in (float("nan"), float("inf"), None, "abc"):
        assert score_of(bad, 0, 1) == 50
    assert score_of(0.5, 0, 1) == 50
    assert score_of(5, 0, 1) == 100   # 超出上界夹到 100
    assert score_of(-5, 0, 1) == 0    # 低于下界夹到 0


def test_merge_config_fills_defaults_and_ignores_unknown() -> None:
    """部分配置 → 其余字段补默认; 未知 key 丢弃 (不因脏配置崩)。"""
    merged = merge_config({
        "dimensions": {
            "index": {"weight": 3.0, "terms": [{"key": "avg_index_pct", "low": -5}]},
            "no_such_dim": {"weight": 9},
        },
        "thresholds": {"strong": 80},
        "unknown_top": 1,
    })
    assert merged["dimensions"]["index"]["weight"] == 3.0
    assert merged["dimensions"]["index"]["terms"][0]["low"] == -5
    assert merged["dimensions"]["index"]["terms"][0]["high"] == 2.5   # 未提及 → 默认
    assert "no_such_dim" not in merged["dimensions"]
    assert "unknown_top" not in merged
    assert merged["thresholds"]["strong"] == 80
    assert merged["thresholds"]["warm"] == DEFAULT_CONFIG["thresholds"]["warm"]


def test_thresholds_normalized_to_descending() -> None:
    """用户把阈值填乱序 → 自动重排为降序, 保证档位区间非空。"""
    merged = merge_config({"thresholds": {"strong": 20, "warm": 90, "neutral": 50, "cold": 10}})
    th = merged["thresholds"]
    assert th["strong"] >= th["warm"] >= th["neutral"] >= th["cold"]


def test_thresholds_clamped_to_range() -> None:
    """阈值越界 (负数/超 100) 夹回 0..100。"""
    merged = merge_config({"thresholds": {"strong": 999, "cold": -50}})
    th = merged["thresholds"]
    assert 0 <= th["cold"] <= 100
    assert 0 <= th["strong"] <= 100


def test_dimension_weight_affects_score() -> None:
    """改维度权重必须改变总分 (否则配置形同虚设)。"""
    m = _sample(random.Random(1))
    base = compute(m, None)["score"]
    heavy = merge_config({"dimensions": {"index": {"weight": 10.0}}})
    assert compute(m, heavy)["score"] != base


def test_term_weight_within_dimension_is_normalized() -> None:
    """子项权重和不为 1 时按比例归一化, 不因总和变化而畸变。

    把「指数」维度 (单子项) 的权重从 1 改成 7, 该维度得分应不变 —— 单子项
    归一化后权重的绝对大小不影响自身得分。
    """
    dim = merge_config(None)["dimensions"]["index"]
    before = dimension_value(dim, {"avg_index_pct": 1.0})
    scaled = merge_config({"dimensions": {"index": {"terms": [
        {"key": "avg_index_pct", "weight": 7.0}]}}})["dimensions"]["index"]
    assert dimension_value(scaled, {"avg_index_pct": 1.0}) == before


def test_missing_metric_takes_neutral() -> None:
    """指标缺失 → 该维度取中性 50 (与历史「无主线数据取 50」一致)。"""
    dim = merge_config(None)["dimensions"]["mainline"]
    assert dimension_value(dim, {}) == 50


def test_public_config_shape() -> None:
    """配置接口返回结构稳定 (前端表单完全依赖它渲染)。"""
    payload = public_config(None)
    assert payload["dimension_order"] == list(DIMENSION_ORDER)
    assert set(payload["config"]) == {"dimensions", "thresholds"}
    assert payload["defaults"] == DEFAULT_CONFIG
    names = [l["name"] for l in payload["labels"]]
    assert "强势" in names and "冰点" in names
    # 每个维度都带 label / weight / terms, 每个 term 带完整六字段
    for key in DIMENSION_ORDER:
        dim = payload["config"]["dimensions"][key]
        assert dim["label"] and "weight" in dim and dim["terms"]
        for term in dim["terms"]:
            assert set(term) == {"key", "label", "weight", "low", "high", "unit"}


def test_compute_returns_dimension_details() -> None:
    """compute 额外返回 dimensions (带权重与 invert), 供配置面板展示。"""
    res = compute(_sample(random.Random(3)), None)
    assert len(res["dimensions"]) == len(DIMENSION_ORDER)
    resilience = next(d for d in res["dimensions"] if d["key"] == "resilience")
    assert resilience["invert"] is True
    assert res["label"] in ("强势", "偏暖", "震荡", "偏冷", "冰点")
