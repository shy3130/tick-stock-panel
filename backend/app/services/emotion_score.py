"""情绪评分 — 维度 / 权重 / 区间的单一权威定义 + 可配置计算。

口径即配置: 6 个维度各自由若干「指标 → 归一化区间 → 加权」子项合成, 维度再按
权重加权平均得到总分; 档位阈值把分数映射为 强势/偏暖/震荡/偏冷/冰点。

设计约束:
  - DEFAULT_CONFIG 复现历史硬编码口径, 逐位一致 (所有子项权重和 = 1, 维度等权);
  - 子项权重在维度内**按权重和归一化**, 因此用户改动权重不会因和不为 1 而畸变;
  - 用户配置与默认配置做逐字段合并, 新增维度/子项时老配置自动补默认值, 不会
    因缺字段而 KeyError;
  - 指标值缺失 (None) 的维度取中性 50, 与历史实现「无主线数据取 50」一致。

消费方: app.services.market_overview_builder (看板总览 + 复盘情绪分)。
"""
from __future__ import annotations

import math

# 档位: (阈值键, 展示名, 颜色语义) — 从高到低匹配
LABELS: tuple[tuple[str, str], ...] = (
    ("strong", "强势"),
    ("warm", "偏暖"),
    ("neutral", "震荡"),
    ("cold", "偏冷"),
)
FALLBACK_LABEL = "冰点"
NEUTRAL = 50


def _default_config() -> dict:
    """出厂口径 — 与改造前的硬编码计算逐位一致。"""

    def dim(label: str, terms: list[dict], **extra) -> dict:
        return {"label": label, "weight": 1.0, "terms": terms, **extra}

    def term(key: str, label: str, weight: float, low: float, high: float, unit: str = "") -> dict:
        return {"key": key, "label": label, "weight": weight, "low": low, "high": high, "unit": unit}

    return {
        "dimensions": {
            "index": dim("指数", [
                term("avg_index_pct", "指数平均涨跌", 1.0, -2.5, 2.5, "%"),
            ]),
            "profit": dim("赚钱", [
                term("up_pct", "上涨占比", 0.45, 20, 80, "%"),
                term("avg_pct", "平均涨跌", 0.25, -0.02, 0.02),
                term("median_pct", "中位涨跌", 0.20, -0.02, 0.02),
                term("strong_diff_pct", "强涨-强跌占比差", 0.10, -8, 8, "%"),
            ]),
            "money": dim("量能", [
                term("avg_vol_ratio", "平均量比", 0.70, 0.6, 1.8),
                term("high_vol_pct", "放量股占比", 0.30, 2, 12, "%"),
            ]),
            "speculation": dim("投机", [
                term("limit_up", "涨停家数", 0.25, 5, 90, "家"),
                term("seal_rate", "封板率", 0.35, 30, 85, "%"),
                term("max_boards", "最高连板", 0.25, 1, 8, "板"),
                term("tier2_count", "2板及以上", 0.15, 0, 30, "家"),
            ]),
            # 抗跌为逆向维度: 下跌越多分越低, 故 invert
            "resilience": dim("抗跌", [
                term("down_pct", "下跌占比", 0.55, 20, 80, "%"),
                term("strong_down_pct", "强跌占比", 0.45, 1, 12, "%"),
            ], invert=True),
            "mainline": dim("主线", [
                term("mainline_avg", "主线平均涨跌", 0.65, -0.005, 0.03),
                term("mainline_cover_pct", "主线覆盖率", 0.35, 1, 12, "%"),
            ]),
        },
        "thresholds": {"strong": 70, "warm": 55, "neutral": 45, "cold": 30},
    }


DEFAULT_CONFIG: dict = _default_config()

# 维度展示顺序 = 雷达图顶点顺序 = 配置面板行序 (三者共用, 避免各处再排一遍)
DIMENSION_ORDER: tuple[str, ...] = tuple(DEFAULT_CONFIG["dimensions"])


def _finite(value) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def score_of(value, low: float, high: float) -> int:
    """指标 → 0..100 归一化分。与历史 `_score` 完全同语义 (含非有限输入取中性)。"""
    if high <= low:
        return NEUTRAL
    v = _finite(value)
    if v is None:
        return NEUTRAL
    return max(0, min(100, round((v - low) / (high - low) * 100)))


def merge_config(raw: dict | None) -> dict:
    """用户配置 → 完整配置 (逐字段合并, 缺项补默认)。

    合并粒度到 term: 用户改过的权重/区间生效, 未提及的字段沿用出厂值。
    维度/子项数量与 key 以出厂定义为准 (不支持增删指标 —— 指标值由计算方提供,
    凭空新增的 key 取不到数)。
    """
    merged = _default_config()
    if not isinstance(raw, dict):
        return merged

    raw_dims = raw.get("dimensions")
    if isinstance(raw_dims, dict):
        for key, dim in merged["dimensions"].items():
            user_dim = raw_dims.get(key)
            if not isinstance(user_dim, dict):
                continue
            weight = _finite(user_dim.get("weight"))
            if weight is not None and weight >= 0:
                dim["weight"] = weight
            user_terms = user_dim.get("terms")
            if not isinstance(user_terms, list):
                continue
            by_key = {t["key"]: t for t in user_terms if isinstance(t, dict)}
            for term in dim["terms"]:
                user_term = by_key.get(term["key"])
                if not isinstance(user_term, dict):
                    continue
                for field in ("weight", "low", "high"):
                    val = _finite(user_term.get(field))
                    if val is not None:
                        term[field] = val

    raw_th = raw.get("thresholds")
    if isinstance(raw_th, dict):
        for key in merged["thresholds"]:
            val = _finite(raw_th.get(key))
            if val is not None:
                merged["thresholds"][key] = val
    _normalize_thresholds(merged["thresholds"])
    return merged


def _normalize_thresholds(th: dict) -> None:
    """阈值夹到 0..100 并保证降序 (强≥暖≥震荡≥冷)。

    用户可能把 强势 填得比 偏暖 还低, 那样档位区间为空、标签永远错。按数值重排
    回填, 既保留用户意图的相对关系, 也保证映射单调。
    """
    for key in th:
        val = _finite(th.get(key))
        th[key] = NEUTRAL if val is None else max(0.0, min(100.0, val))
    values = sorted((th[k] for k in th), reverse=True)
    for (key, _), val in zip(LABELS, values):
        th[key] = val


def label_of(score: float, thresholds: dict) -> str:
    for key, name in LABELS:
        if score >= thresholds.get(key, 0):
            return name
    return FALLBACK_LABEL


def dimension_value(dim: dict, metrics: dict) -> int:
    """单个维度 → 0..100。子项权重和归一化; 全部子项缺数时取中性。"""
    total_weight = 0.0
    acc = 0.0
    for term in dim["terms"]:
        if term["key"] not in metrics:
            continue   # 该指标本次不可用 → 跳过, 不当作 0
        value = score_of(metrics[term["key"]], term["low"], term["high"])
        weight = max(0.0, float(term.get("weight") or 0))
        if weight <= 0:
            continue
        acc += value * weight
        total_weight += weight
    if total_weight <= 0:
        return NEUTRAL
    # 权重和已是 1 时不做除法: 出厂口径的 0.45+0.25+0.20+0.10 在浮点下是
    # 0.9999999999999999, 除一趟会把恰好落在 .5 的值推过舍入边界, 使默认结果
    # 与历史实现差 1 分。改过权重时才归一化 (此时结果本就该变)。
    weighted = acc if abs(total_weight - 1.0) < 1e-9 else acc / total_weight
    if dim.get("invert"):
        # 先取整再取反 (而非 round(100 - x)): 两者在 x 的小数部分恰为 .5 时
        # 结果可差 1, 出厂口径必须与历史实现逐位一致。
        return 100 - round(weighted)
    return round(weighted)


def compute(metrics: dict, config: dict | None = None) -> dict:
    """指标 + 配置 → {score, label, radar, dimensions}。

    radar 保持历史形状 [{key,label,value}] (前端雷达图与复盘快照依赖),
    dimensions 额外带权重与命中分, 供配置面板展示「各维度当前实际得分」。
    """
    cfg = merge_config(config)
    radar: list[dict] = []
    dims: list[dict] = []
    for key in DIMENSION_ORDER:
        dim = cfg["dimensions"][key]
        value = dimension_value(dim, metrics)
        radar.append({"key": key, "label": dim["label"], "value": value})
        dims.append({
            "key": key,
            "label": dim["label"],
            "value": value,
            "weight": dim["weight"],
            "invert": bool(dim.get("invert")),
        })

    # 等权时直接取整数均值: 权重在均值里本就约掉, 而 Python 3.12+ 的 sum() 对
    # 浮点走补偿求和、对整数是精确累加 —— 走 `int * 1.0` 会引入浮点误差, 让恰好
    # 落在 .5 的总分被舍到另一侧, 与历史实现差 1 分。改过权重时才走加权分支。
    weights = [d["weight"] for d in dims]
    if weights and all(abs(w - weights[0]) < 1e-12 for w in weights):
        score = round(sum(d["value"] for d in dims) / len(dims)) if dims else NEUTRAL
    else:
        total_weight = sum(weights)
        if total_weight > 0:
            score = round(sum(d["value"] * d["weight"] for d in dims) / total_weight)
        elif dims:
            score = round(sum(d["value"] for d in dims) / len(dims))
        else:
            score = NEUTRAL

    return {
        "score": score,
        "label": label_of(score, cfg["thresholds"]),
        "radar": radar,
        "dimensions": dims,
        "thresholds": cfg["thresholds"],
    }


def public_config(config: dict | None = None) -> dict:
    """完整配置 + 指标元数据 (供前端渲染配置面板)。"""
    cfg = merge_config(config)
    return {
        "config": cfg,
        "defaults": DEFAULT_CONFIG,
        "dimension_order": list(DIMENSION_ORDER),
        "labels": [{"key": k, "name": n} for k, n in LABELS] + [{"key": "fallback", "name": FALLBACK_LABEL}],
    }
