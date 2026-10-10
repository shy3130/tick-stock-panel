"""自定义信号 API 路由 — HTTP 请求 → 调用 custom_signals 模块 → 返回响应。

只做胶水：校验 → 持久化 → 失效缓存。不含表达式编译逻辑。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.strategy import custom_signals
from app.strategy.intraday_features import INTRADAY_FEATURES

router = APIRouter(prefix="/api/custom-signals", tags=["custom-signals"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _invalidate(request: Request) -> None:
    """失效自定义信号表达式缓存, 并清掉含旧信号列的计算缓存。

    信号增删会改变注入列集合: 只清表达式缓存不够, repo 内存缓存 /
    strategy 磁盘缓存里算好的历史窗口仍不含新 csg_ 列 (或仍含已删列),
    需要一并清除, 否则创建信号后立即运行策略仍会报缺列。
    盘中信号定义缓存(intraday)一并失效, 下一分钟 bucket 即生效。
    """
    custom_signals.invalidate_intraday_cache()
    from app.indicators.pipeline import invalidate_custom_signals
    invalidate_custom_signals()
    from app.services import strategy_cache
    strategy_cache.clear_cache(_data_dir(request))
    repo = request.app.state.repo
    if hasattr(repo, "clear_cache"):
        repo.clear_cache()


class ConditionModel(BaseModel):
    left: str        # 字段名（日线在白名单 / 盘中在特征白名单）
    op: str          # > >= < <= == != ; 盘中额外: cross_up cross_down
    right: str       # "field:xxx" 或数字字符串
    leftDays: int = 0    # 左字段取几日前 (0=当日, 默认; 盘中信号必须为 0)
    rightDays: int = 0   # 右字段取几日前 (仅 right 为字段时有意义; 盘中信号必须为 0)


class SignalModel(BaseModel):
    id: str
    name: str
    kind: str        # entry | exit | both
    conditions: list[ConditionModel]
    enabled: bool = True
    timeframe: str = "daily"   # daily | intraday(分钟K特征, 输出当日条件上升沿)
    min_bars: int = 0          # 仅 intraday: 当日最少已完成 bar 数, 不足不触发
    # 显式输出列名 (None → csg_{id}); 迁移自内置的定义沿用 signal_* 原列名,
    # 编辑保存时必须原样回传, 否则列名漂移会静默断开下游引用。
    column: str | None = None


class IntradayReplayRequest(BaseModel):
    """盘中信号历史回放 — 用本地分钟K重放触发时点, 不消耗盘中数据能力。"""
    signal_id: str
    start_date: str   # YYYY-MM-DD
    end_date: str     # YYYY-MM-DD
    symbols: list[str]
    asset_type: str = "stock"


class AIGenerateRequest(BaseModel):
    description: str


# ── 字段选项 / 运算符 ───────────────────────────────────


@router.get("/options")
def get_options():
    """返回可选字段与运算符，供前端下拉框使用。"""
    # 字段带中文标签（取自 ENRICHED_COLUMNS，回退为字段名本身）
    from app.indicators.pipeline import ENRICHED_COLUMNS, ENRICHED_COLUMNS_BY_CATEGORY

    allowed = custom_signals.ALLOWED_FIELDS
    fields = [
        {"key": f, "label": ENRICHED_COLUMNS.get(f, f)}
        for f in sorted(allowed)
    ]
    # 字段分组 (只包含白名单内的字段, 供前端 optoptgroup 渲染)
    _GROUP_LABELS = {
        "basic": "基础", "ma": "均线 MA", "ema": "指数均线 EMA",
        "macd": "MACD", "boll": "布林带 BOLL", "kdj": "KDJ",
        "atr": "ATR", "volume": "量价", "extremes": "极值",
        "momentum": "动量", "volatility": "波动率", "rsi": "RSI",
    }
    # 行情类字段不在 ENRICHED_COLUMNS_BY_CATEGORY 里, 单独归一组
    quote_fields = {"open", "high", "low", "close", "volume", "amount",
                    "turnover_rate", "consecutive_limit_ups", "consecutive_limit_downs"}
    groups = [{"key": "quote", "label": "行情",
               "fields": [{"key": f, "label": ENRICHED_COLUMNS.get(f, f)}
                          for f in sorted(allowed & quote_fields)]}]
    for cat, label in _GROUP_LABELS.items():
        cat_fields = [f for f in ENRICHED_COLUMNS_BY_CATEGORY.get(cat, []) if f in allowed]
        if cat_fields:
            groups.append({"key": cat, "label": label,
                           "fields": [{"key": f, "label": ENRICHED_COLUMNS.get(f, f)} for f in cat_fields]})

    # 注册表因子 (虚拟/自定义/复合): 历史路径由 compute_signals 复用评分物化
    # 管线补算; 已是物化列的基础因子 (rsi_14 等) 上面已分组, 此处跳过。
    from app.factors.registry import all_factors

    factor_groups: dict[str, list[dict[str, str]]] = {}
    for spec in all_factors():
        if spec.id in allowed:
            continue
        label = spec.label
        if spec.warmup_bars > 1:
            label = f"{label} · 预热{spec.warmup_bars}日"
        if list(spec.asset_types) == ["stock"]:
            label = f"{label} · 仅股票"
        factor_groups.setdefault(spec.group or "因子", []).append({"key": spec.id, "label": label})
    for group_label, group_fields in factor_groups.items():
        groups.append({"key": f"factor:{group_label}", "label": f"因子 · {group_label}", "fields": group_fields})
        fields.extend(group_fields)

    # string 扩展字段 (概念/行业归属等): 只进信号条件, 不注册为因子。
    # stringFields 标记 + 独立分组, 前端据此切换运算符 (包含/等于/不等于)
    # 与右值输入 (字符串文本, 不支持字段引用)。
    from app.factors.ext_factors import ext_string_field_entries

    str_entries = ext_string_field_entries()
    if str_entries:
        str_group = {"key": "ext_string", "label": "扩展 · 字符串", "fields": str_entries}
        groups.append(str_group)
        fields.extend(str_entries)

    return {
        "fields": fields,
        "groups": groups,
        "maxDays": custom_signals.MAX_DAYS,
        "operators": [">", ">=", "<", "<=", "==", "!="],
        "stringFields": [e["key"] for e in str_entries],
        "stringOperators": ["contains", "==", "!="],
        "kinds": [
            {"key": "entry", "label": "入场"},
            {"key": "exit", "label": "出场"},
            {"key": "both", "label": "出入通用"},
        ],
        # 盘中信号(timeframe=intraday): 分钟K特征白名单 + 额外穿越算子
        "intraday": {
            "fields": [
                {"key": f, "label": label}
                for f, label in sorted(INTRADAY_FEATURES.items())
            ],
            "operators": [">", ">=", "<", "<=", "==", "!=", "cross_up", "cross_down"],
        },
        "timeframes": [
            {"key": "daily", "label": "日线"},
            {"key": "intraday", "label": "盘中(分钟K)"},
        ],
    }


# ── 列表 ───────────────────────────────────────────────


@router.get("")
def list_signals(request: Request):
    sigs = custom_signals.load_all(_data_dir(request))
    return {"signals": sigs}


# ── 新建 / 更新 ────────────────────────────────────────


@router.post("")
def save_signal(req: SignalModel, request: Request):
    sig = req.model_dump()
    try:
        custom_signals.validate(sig)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 显式列名冲突闸: 两个定义输出同一列 (如改名另存仍带 signal_* column)
    # 会让后者静默覆盖前者的注入, 必须显式拒绝。
    if sig.get("column"):
        for existing in custom_signals.load_all(_data_dir(request)):
            if existing["id"] != sig["id"] and (
                existing.get("column") == sig["column"]
                or (not existing.get("column") and f"csg_{existing['id']}" == sig["column"])
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"列名 {sig['column']} 已被信号 {existing['id']} 占用",
                )
    custom_signals.save_one(_data_dir(request), sig)
    _invalidate(request)
    return {"ok": True, "signal": sig}


# ── AI 生成 ─────────────────────────────────────────────


@router.post("/ai/generate")
async def ai_generate_signal(req: AIGenerateRequest):
    """AI 根据自然语言描述生成自定义信号条件。

    不落盘：只返回 {name, conditions} 供前端回填表单，由用户确认后走
    常规 save 流程。校验复用 custom_signals.validate()（白名单安全闸门）。
    """
    from app.services.ai_provider import generate_ai_text
    from app.strategy import custom_signals_ai

    description = req.description.strip()
    if not description:
        raise HTTPException(status_code=400, detail="请先描述信号思路")
    if len(description) > 500:
        raise HTTPException(status_code=400, detail="描述过长（最多 500 字）")

    messages = custom_signals_ai.build_messages(description)
    try:
        # max_tokens=None 不传上限: 推理模型思考 token 计入预算, 显式限制
        # 会挤占正文导致 JSON 截断/0 字 (与四个分析器同因, 见 0ee3aa8)
        text = await generate_ai_text(messages, temperature=0.2, max_tokens=None)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"AI 生成失败: {e}") from e

    try:
        return custom_signals_ai.parse_and_validate(text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ── 删除 ───────────────────────────────────────────────


@router.delete("/{signal_id}")
def delete_signal(signal_id: str, request: Request):
    if not custom_signals.ID_RE.match(signal_id):
        raise HTTPException(status_code=400, detail="信号 id 非法")
    deleted = custom_signals.delete_one(_data_dir(request), signal_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="信号不存在")
    _invalidate(request)
    return {"ok": True}


# ── 盘中信号历史回放 ────────────────────────────────────


@router.post("/intraday/replay")
def intraday_replay(req: IntradayReplayRequest, request: Request):
    """用本地历史分钟K回放盘中信号的触发时点。

    只读本地分钟分区, 不消耗盘中数据能力 — 用户可先在历史区间验证信号,
    再决定是否配置到监控/分钟策略。昨收取自本地日K(无昨日数据的日子该特征降级)。
    """
    from datetime import date, timedelta

    import polars as pl

    from app.strategy.intraday_features import build_feature_frame

    try:
        start = date.fromisoformat(req.start_date)
        end = date.fromisoformat(req.end_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"日期格式错误: {e}") from e
    if start > end:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
    if (end - start).days > 60:
        raise HTTPException(status_code=400, detail="回放区间最长 60 天")
    symbols = [s for s in dict.fromkeys(req.symbols) if s]
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols 不能为空")
    if len(symbols) > 200:
        raise HTTPException(status_code=400, detail="单次回放最多 200 只标的")

    # 信号定义必须存在且为盘中类型
    sig = next(
        (s for s in custom_signals.load_all(_data_dir(request)) if s.get("id") == req.signal_id),
        None,
    )
    if sig is None:
        raise HTTPException(status_code=404, detail="信号不存在")
    if sig.get("timeframe") != custom_signals.TIMEFRAME_INTRADAY:
        raise HTTPException(status_code=400, detail="该信号不是盘中(timeframe=intraday)信号")
    exprs = custom_signals.build_intraday_expressions([sig])
    col = custom_signals.intraday_column_name(sig["id"])
    if col not in exprs:
        raise HTTPException(status_code=400, detail="信号编译失败, 请检查条件字段")
    min_bars = int(sig.get("min_bars", 0) or 0)

    repo = request.app.state.repo
    # 昨收映射: 一次性取区间(含前置 15 天)日K, 按「严格早于当日」取最近收盘
    daily = repo.get_daily_batch(symbols, start - timedelta(days=15), end, columns=["symbol", "date", "close"])
    close_by_sym_date: dict[str, dict[date, float]] = {}
    if not daily.is_empty():
        for row in daily.sort(["symbol", "date"]).iter_rows(named=True):
            close_by_sym_date.setdefault(str(row["symbol"]), {})[row["date"]] = float(row["close"])

    triggers: list[dict] = []
    days_scanned = 0
    bars_scanned = 0
    day = start
    while day <= end:
        minute_df = repo.get_minute_batch(symbols, day, asset_type=req.asset_type)
        if minute_df is not None and not minute_df.is_empty():
            days_scanned += 1
            bars_scanned += minute_df.height
            prev_close = {
                sym: closes_map[max(d for d in closes_map if d < day)]
                for sym, closes_map in close_by_sym_date.items()
                if any(d < day for d in closes_map)
            }
            frame = build_feature_frame(minute_df, prev_close=prev_close)
            if not frame.is_empty():
                evaluated = custom_signals.apply_intraday_edges(frame, {col: exprs[col]}).with_columns(
                    pl.int_range(pl.len()).over(["symbol", "date"]).alias("_bar_idx")
                )
                if min_bars > 0:
                    evaluated = evaluated.with_columns(
                        pl.when(pl.col("_bar_idx") + 1 >= min_bars)
                        .then(pl.col(col))
                        .otherwise(False)
                        .alias(col)
                    )
                for row in evaluated.filter(pl.col(col)).sort(["datetime", "symbol"]).iter_rows(named=True):
                    triggers.append({
                        "date": day.isoformat(),
                        "time": str(row["datetime"].time()),
                        "symbol": row["symbol"],
                    })
        day += timedelta(days=1)

    return {
        "signal_id": req.signal_id,
        "start_date": req.start_date,
        "end_date": req.end_date,
        "symbols": symbols,
        "days_scanned": days_scanned,
        "bars_scanned": bars_scanned,
        "triggers": triggers,
    }
