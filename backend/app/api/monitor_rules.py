"""监控规则 API 路由 — HTTP 请求 → 调用 monitor_rules 模块 → 同步引擎内存态。

只做胶水: 校验 → 持久化 → 失效引擎内存态。不含评估逻辑。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.strategy import monitor_rules
from app.strategy.intraday_signals import INTRADAY_SIGNAL_LABELS, uses_intraday_signals

router = APIRouter(prefix="/api/monitor-rules", tags=["monitor-rules"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _sync_engine(request: Request) -> None:
    """保存/删除后,把最新规则集 reload 到引擎内存态。"""
    engine = getattr(request.app.state, "monitor_engine", None)
    if engine is not None:
        rules = monitor_rules.load_all(_data_dir(request))
        engine.set_rules(rules)


# ── Pydantic 模型 ───────────────────────────────────────
class ConditionModel(BaseModel):
    field: str
    op: str            # truth | > >= < <= == !=
    value: float | None = None   # op 非 truth 时必填


class RuleModel(BaseModel):
    id: str
    name: str
    enabled: bool = True
    type: str          # strategy | signal | price | market
    asset_type: str = "stock"   # stock | etf (etf: strategy 型走 ETF 历史加载器)
    scope: str = "symbols"   # symbols | all | sector
    symbols: list[str] = []
    sector: str | None = None
    strategy_id: str | None = None
    direction: str = "entry"  # entry | exit | both
    conditions: list[ConditionModel] = []
    logic: str = "and"        # and | or
    cooldown_seconds: int = 3600
    severity: str = "info"    # info | warn | critical
    webhook_url: str = ""     # Webhook 推送地址 (推送到 QMT 等外部软件, 待定)
    webhook_enabled: bool = False  # 兼容老规则 (已由 webhook_channels 取代, 仅做向后兼容读)
    webhook_channels: list[str] = []  # 命中时推送的外部渠道 (合法值 'feishu' | 'wecom')
    message: str = ""
    # ladder 专属 (连板梯队封单监控)
    metric: str = "sealed_vol"   # sealed_vol=封单量(手) | sealed_amount=封单额(元)
    threshold: float = 0         # 封单 <= 此值时报警 (原始单位: 量=手, 额=元)


# ── 字段选项 ─────────────────────────────────────────────
@router.get("/options")
def get_options(request: Request):
    """返回可选字段、信号列、运算符、枚举,供前端表单使用。"""
    from app.indicators.pipeline import ENRICHED_COLUMNS
    from app.services.kline_sync import intraday_monitor_support
    from app.strategy.custom_signals import ALLOWED_FIELDS, load_all as load_csg

    # 阈值字段 (带中文标签)
    threshold_fields = [
        {"key": f, "label": ENRICHED_COLUMNS.get(f, f)}
        for f in sorted(ALLOWED_FIELDS)
    ]
    # 内置信号列 (布尔, 用于 op=truth)
    builtin_signals = [
        {"key": k, "label": v}
        for k, v in ENRICHED_COLUMNS.items()
        if k.startswith("signal_")
    ]
    builtin_signals.extend(
        {"key": key, "label": label}
        for key, label in INTRADAY_SIGNAL_LABELS.items()
    )
    # 自定义信号列 (csg_)
    custom_sigs = []
    try:
        for cs in load_csg(_data_dir(request)):
            if cs.get("enabled") is not False:
                custom_sigs.append({
                    "key": f"csg_{cs['id']}",
                    "label": cs.get("name", cs["id"]),
                })
    except Exception:
        pass

    return {
        "threshold_fields": threshold_fields,
        "builtin_signals": builtin_signals,
        "custom_signals": custom_sigs,
        "operators": [">", ">=", "<", "<=", "==", "!="],
        "types": [
            {"key": "signal", "label": "个股信号"},
            {"key": "price", "label": "价格/涨跌"},
            {"key": "market", "label": "市场异动"},
            {"key": "strategy", "label": "策略监控"},
        ],
        "scopes": [
            {"key": "symbols", "label": "指定股票"},
            {"key": "all", "label": "全市场"},
            {"key": "sector", "label": "板块"},
        ],
        "logics": [
            {"key": "and", "label": "全部满足 (AND)"},
            {"key": "or", "label": "任一满足 (OR)"},
        ],
        "severities": [
            {"key": "info", "label": "普通"},
            {"key": "warn", "label": "警告"},
            {"key": "critical", "label": "重要"},
        ],
        "directions": [
            {"key": "entry", "label": "入场"},
            {"key": "exit", "label": "出场"},
            {"key": "both", "label": "出入都报"},
        ],
        "intraday_signal_support": intraday_monitor_support(
            getattr(request.app.state, "capabilities", None),
        ),
    }


# ── 列表 ───────────────────────────────────────────────
@router.get("")
def list_rules(request: Request):
    rules = monitor_rules.load_all(_data_dir(request))
    from app.services.kline_sync import intraday_monitor_support

    support = intraday_monitor_support(getattr(request.app.state, "capabilities", None))
    intraday_rules = [
        rule for rule in rules
        if rule.get("enabled", True) and uses_intraday_signals(rule)
    ]
    pooled_symbols = {
        str(symbol)
        for rule in intraday_rules
        for symbol in rule.get("symbols", [])
        if symbol
    }
    runtime_warning = ""
    if intraday_rules and not support["available"]:
        runtime_warning = str(support["reason"])
    elif len(pooled_symbols) > int(support["max_symbols"]):
        runtime_warning = (
            f"分时监听标的池已超限: {len(pooled_symbols)}/{support['max_symbols']}"
        )
    if runtime_warning:
        for rule in intraday_rules:
            rule["runtime_warning"] = runtime_warning
    # 按 created_at 倒序
    rules.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return {"rules": rules}


# ── 新建 / 更新 ────────────────────────────────────────
@router.post("")
def save_rule(req: RuleModel, request: Request):
    rule = monitor_rules.normalize(req.model_dump())
    # 连板梯队封单监控 (type=ladder) 依赖五档盘口数据, 需 Pro+ (DEPTH5_BATCH 能力)。
    # 无能力时拒绝创建, 避免规则存了却永远无法触发。
    if rule.get("type") == "ladder":
        from app.tickflow.capabilities import Cap
        capset = getattr(request.app.state, "capabilities", None)
        if capset is None or not capset.has(Cap.DEPTH5_BATCH):
            raise HTTPException(
                status_code=403,
                detail="封单监控需要 Pro+ 套餐 (批量五档能力),请升级后在「设置」页配置",
            )
    if rule.get("type") == "strategy":
        from app.strategy.engine import StrategyDataContext

        strategy_engine = getattr(request.app.state, "strategy_engine", None)
        if strategy_engine is None:
            raise HTTPException(status_code=503, detail="策略引擎未初始化")
        try:
            strategy = strategy_engine.get(str(rule.get("strategy_id")))
            strategy_engine.validate_context(
                strategy,
                StrategyDataContext(
                    asset_type=str(rule.get("asset_type") or "stock"),
                    timeframe="1d",
                    as_of=date.today(),
                ),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    # 编辑现有规则时, 保留原 created_at (避免按时间排序时位置跳动)
    existing = monitor_rules.load_one(_data_dir(request), rule["id"])
    if existing and existing.get("created_at"):
        rule["created_at"] = existing["created_at"]
    try:
        monitor_rules.validate(rule)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if rule.get("enabled", True) and uses_intraday_signals(rule):
        from app.services.kline_sync import intraday_monitor_support

        support = intraday_monitor_support(getattr(request.app.state, "capabilities", None))
        if not support["available"]:
            raise HTTPException(status_code=403, detail=str(support["reason"]))
        symbols = set(str(symbol) for symbol in rule.get("symbols", []) if symbol)
        for saved in monitor_rules.load_all(_data_dir(request)):
            if (
                saved.get("id") != rule.get("id")
                and saved.get("enabled", True)
                and uses_intraday_signals(saved)
            ):
                symbols.update(str(symbol) for symbol in saved.get("symbols", []) if symbol)
        max_symbols = int(support["max_symbols"])
        if len(symbols) > max_symbols:
            raise HTTPException(
                status_code=400,
                detail=f"当前分时数据能力最多监听 {max_symbols} 只标的,当前规则合计 {len(symbols)} 只",
            )
    monitor_rules.save_one(_data_dir(request), rule)
    _sync_engine(request)
    return {"ok": True, "rule": rule}


# ── 删除 ───────────────────────────────────────────────
@router.delete("/{rule_id}")
def delete_rule(rule_id: str, request: Request):
    if not monitor_rules.ID_RE.match(rule_id):
        raise HTTPException(status_code=400, detail="规则 id 非法")
    deleted = monitor_rules.delete_one(_data_dir(request), rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="规则不存在")
    _sync_engine(request)
    return {"ok": True}


# ── 演示数据生成 (仅 Dev 页用) ─────────────────────────

import time as _time
from datetime import datetime, timezone


def _demo_rule(rule_id: str, name: str, rtype: str, scope: str, symbols: list[str],
               conditions: list[dict], logic: str = "or", cooldown: int = 3600,
               severity: str = "info", message: str = "",
               strategy_id: str | None = None, direction: str = "entry") -> dict:
    rule = monitor_rules.normalize({
        "id": rule_id,
        "name": name,
        "type": rtype,
        "scope": scope,
        "symbols": symbols,
        "conditions": conditions,
        "logic": logic,
        "cooldown_seconds": cooldown,
        "severity": severity,
        "message": message,
        "enabled": True,
    })
    if rtype == "strategy":
        rule["strategy_id"] = strategy_id
        rule["direction"] = direction
    return rule


_DEMO_RULES_TEMPLATE = [
    ("个股信号 · 茅台放量突破", "signal", "symbols", ["600519.SH"],
     [{"field": "signal_volume_surge", "op": "truth"},
      {"field": "signal_n_day_high", "op": "truth"}], "or", "info"),
    ("个股信号 · 宁德金叉", "signal", "symbols", ["300750.SZ"],
     [{"field": "signal_ma_golden_5_20", "op": "truth"}], "or", "info"),
    ("价格 · 平安跌幅监控", "price", "symbols", ["000001.SZ"],
     [{"field": "change_pct", "op": "<", "value": -0.03}], "or", "warn", "warn"),
    ("价格 · 比亚迪RSI超卖", "price", "symbols", ["002594.SZ"],
     [{"field": "rsi_14", "op": "<", "value": 30}], "and", "warn", "warn"),
    ("市场异动 · 全市场涨停", "market", "all", [],
     [{"field": "signal_limit_up", "op": "truth"}], "or", "critical", "critical"),
    ("市场异动 · 全市场炸板", "market", "all", [],
     [{"field": "signal_broken_limit_up", "op": "truth"}], "or", "warn", "warn"),
    ("市场异动 · 跌幅超5%", "market", "all", [],
     [{"field": "change_pct", "op": "<", "value": -0.05}], "or", "warn", "warn"),
    ("个股信号 · 茅台跌破MA20", "signal", "symbols", ["600519.SH"],
     [{"field": "signal_ma20_breakdown", "op": "truth"}], "or", "info"),
]

# 策略类型单独声明 (格式不同: 含 strategy_id + direction)
_DEMO_STRATEGY_RULES: list[dict] = [
    {"name": "策略监控 · 趋势突破", "strategy_id": "trend_breakout", "direction": "entry"},
    {"name": "策略监控 · MACD金叉", "strategy_id": "macd_golden", "direction": "both"},
]


@router.post("/seed")
def seed_demo_rules(request: Request):
    """生成演示监控规则 (Dev 页用)。覆盖 signal/price/market/strategy 四类。"""
    ts = int(_time.time() * 1000)
    created = []
    i = 0
    for (name, rtype, scope, symbols, conditions, logic, severity, sev) in _DEMO_RULES_TEMPLATE:
        rule_id = f"demo_{ts}_{i}"
        rule = _demo_rule(rule_id, name, rtype, scope, symbols, conditions, logic, 3600, sev)
        monitor_rules.save_one(_data_dir(request), rule)
        created.append(rule_id)
        i += 1
    # 策略类型规则
    for sr in _DEMO_STRATEGY_RULES:
        rule_id = f"demo_{ts}_{i}"
        rule = _demo_rule(
            rule_id, sr["name"], "strategy", "all", [], [], "and", 3600, "info",
            strategy_id=sr["strategy_id"], direction=sr.get("direction", "entry"),
        )
        monitor_rules.save_one(_data_dir(request), rule)
        created.append(rule_id)
        i += 1
    _sync_engine(request)
    return {"ok": True, "generated": len(created), "ids": created}


# ── 封单监控模拟触发 (Dev 调试用) ─────────────────────
@router.post("/test-ladder")
def test_ladder(request: Request):
    """模拟触发所有 ladder 规则, 返回命中结果 (不落盘、不推送飞书)。

    用当前 depth_service 的封单数据 + enriched 最新日 close 构造 mock DataFrame,
    跑 _evaluate_ladder 判断哪些规则会触发。供 Dev 页面调试验证。
    """
    import polars as pl

    repo = request.app.state.repo
    depth_svc = getattr(request.app.state, "depth_service", None)
    engine = getattr(request.app.state, "monitor_engine", None)

    if not depth_svc:
        raise HTTPException(status_code=503, detail="depth 服务未初始化")
    if not engine or not engine.has_rule_type("ladder"):
        raise HTTPException(status_code=400, detail="无 ladder 类型监控规则")

    # 最新交易日
    latest = repo.enriched_latest_date()
    if not latest:
        raise HTTPException(status_code=400, detail="无 enriched 数据")

    # 取涨停+跌停封单 {symbol: vol}
    sealed: dict[str, int] = {}
    for is_down in (False, True):
        m = depth_svc.get_sealed_map(latest, is_down=is_down)
        for sym, info in m.items():
            vol = (info or {}).get("vol")
            if vol and vol > 0:
                sealed[sym] = vol

    if not sealed:
        raise HTTPException(status_code=400, detail="无封单数据 (depth 未拉取或无涨停/跌停股)")

    # 取这些 symbol 的 close (算封单额用)
    enriched_today, _ = repo.get_enriched_latest()
    cols = ["symbol", "close", "change_pct"]
    avail = [c for c in cols if c in enriched_today.columns]
    mock = enriched_today.select(avail).filter(pl.col("symbol").is_in(list(sealed.keys())))

    # 注入 _sealed_vol
    sealed_df = pl.DataFrame({
        "symbol": list(sealed.keys()),
        "_sealed_vol": list(sealed.values()),
    })
    mock = mock.join(sealed_df, on="symbol", how="inner")

    # 取所有 ladder 规则, 逐条纯条件判断 (绕过引擎 cooldown, 不污染 _last_fire)
    ladder_rules = [r for r in engine.rules.values() if r.get("type") == "ladder" and r.get("enabled", True)]
    all_events = []
    not_triggered = []

    for rule in ladder_rules:
        syms = rule.get("symbols", [])
        sym = syms[0] if syms else None
        metric = rule.get("metric", "sealed_vol")
        thr = rule.get("threshold", 0)
        direction = rule.get("direction", "up")
        warn_label = "炸板预警" if direction == "up" else "翘板预警"

        # 取该 symbol 的封单数据
        cur_vol = sealed.get(sym) if sym else None
        row = mock.filter(pl.col("symbol") == sym) if sym else mock.clear()
        cur_close = row["close"][0] if len(row) and "close" in row.columns else None
        cur_amt = (cur_vol * 100 * cur_close) if (cur_vol and cur_close) else None
        cur_val = cur_amt if metric == "sealed_amount" else cur_vol

        # 条件判断: 封单 > 0 且 比较值 <= 阈值
        if cur_val is not None and cur_val > 0 and cur_val <= thr:
            if metric == "sealed_amount":
                sv_text = f"{cur_val / 1e4:.0f}万元"
                th_text = f"{thr / 1e4:.0f}万元"
            else:
                sv_text = f"{cur_val:,.0f} 手"
                th_text = f"{thr:,.0f} 手"
            all_events.append({
                "rule_id": rule["id"],
                "rule_name": rule.get("name", ""),
                "symbol": sym,
                "name": sym,
                "type": warn_label,
                "message": f"{warn_label} · 封单 {sv_text} ≤ {th_text}",
                "severity": rule.get("severity", "warn"),
                "sealed_value": cur_val,
                "sealed_metric": metric,
                "current_sealed_vol": cur_vol,
                "current_sealed_amount": cur_amt,
            })
        else:
            reason = "封单数据缺失" if cur_val is None else (
                f"封单 {cur_val:,.0f} > 阈值 {thr:,.0f}" if cur_val > thr else "封单为 0"
            )
            not_triggered.append({
                "rule_id": rule["id"],
                "rule_name": rule.get("name", ""),
                "symbol": sym,
                "metric": metric,
                "threshold": thr,
                "current_value": cur_val,
                "current_sealed_vol": cur_vol,
                "current_sealed_amount": cur_amt,
                "reason": reason,
            })

    return {
        "ok": True,
        "as_of": str(latest),
        "sealed_count": len(sealed),
        "triggered": all_events,
        "not_triggered": not_triggered,
    }


@router.post("/trigger-ladder")
def trigger_ladder(request: Request):
    """真实触发一次 ladder 预警 (落盘 + 飞书推送 + SSE), 供 Dev 调试验证完整效果。

    与 test-ladder 区别: 本端点会真的把预警写入 alerts.jsonl、推送飞书、触发 SSE,
    让用户看到真实的预警通知。绕过 cooldown 强制触发。
    """
    import time
    from app.services import alert_store

    repo = request.app.state.repo
    depth_svc = getattr(request.app.state, "depth_service", None)
    engine = getattr(request.app.state, "monitor_engine", None)
    quote_svc = getattr(request.app.state, "quote_service", None)

    if not depth_svc:
        raise HTTPException(status_code=503, detail="depth 服务未初始化")
    if not engine or not engine.has_rule_type("ladder"):
        raise HTTPException(status_code=400, detail="无 ladder 类型监控规则")

    latest = repo.enriched_latest_date()
    if not latest:
        raise HTTPException(status_code=400, detail="无 enriched 数据")

    # 取封单
    sealed: dict[str, int] = {}
    for is_down in (False, True):
        m = depth_svc.get_sealed_map(latest, is_down=is_down)
        for sym, info in m.items():
            vol = (info or {}).get("vol")
            if vol and vol > 0:
                sealed[sym] = vol
    if not sealed:
        raise HTTPException(status_code=400, detail="无封单数据")

    # 构造真实 rule_events (与 _evaluate_ladder 产出格式一致)
    import polars as pl
    enriched_today, _ = repo.get_enriched_latest()
    cols = [c for c in ["symbol", "close", "change_pct"] if c in enriched_today.columns]
    mock = enriched_today.select(cols).filter(pl.col("symbol").is_in(list(sealed.keys())))
    sealed_df = pl.DataFrame({"symbol": list(sealed.keys()), "_sealed_vol": list(sealed.values())})
    mock = mock.join(sealed_df, on="symbol", how="inner")

    now = time.time()
    rule_events: list[dict] = []
    name_map = {}
    try:
        inst = repo.get_instruments()
        if not inst.is_empty() and "name" in inst.columns:
            name_map = {r["symbol"]: r["name"] for r in inst.select(["symbol", "name"]).iter_rows(named=True) if r.get("name")}
    except Exception:  # noqa: BLE001
        pass

    for rule in engine.rules.values():
        if rule.get("type") != "ladder" or not rule.get("enabled", True):
            continue
        sym = rule.get("symbols", [""])[0] if rule.get("symbols") else ""
        metric = rule.get("metric", "sealed_vol")
        thr = rule.get("threshold", 0)
        direction = rule.get("direction", "up")
        warn_label = "炸板预警" if direction == "up" else "翘板预警"

        row = mock.filter(pl.col("symbol") == sym)
        if row.is_empty():
            continue
        cur_vol = row["_sealed_vol"][0]
        close_v = row["close"][0] if "close" in row.columns else None
        cur_val = cur_vol * 100 * close_v if metric == "sealed_amount" else cur_vol
        if not cur_val or cur_val <= 0 or cur_val > thr:
            continue  # 不满足条件, 跳过

        if metric == "sealed_amount":
            sv_text = f"{cur_val / 1e4:.0f}万元"
            th_text = f"{thr / 1e4:.0f}万元"
        else:
            sv_text = f"{cur_val:,.0f} 手"
            th_text = f"{thr:,.0f} 手"

        rule_events.append({
            "ts": int(now * 1000),
            "rule_id": rule["id"],
            "rule_name": rule.get("name", ""),
            "source": "ladder",
            "type": warn_label,
            "symbol": sym,
            "name": name_map.get(sym, sym),
            "message": f"{warn_label} · 封单 {sv_text} ≤ {th_text}",
            "price": close_v,
            "change_pct": row["change_pct"][0] if "change_pct" in row.columns else None,
            "signals": [],
            "severity": rule.get("severity", "warn"),
            "conditions": [],
            "logic": "and",
            "sealed_value": cur_val,
            "sealed_metric": metric,
        })

    if not rule_events:
        raise HTTPException(status_code=400, detail="当前无 ladder 规则满足触发条件 (封单均 > 阈值)")

    # 1. 落盘到 alerts.jsonl
    try:
        alert_store.append_many(repo.store.data_dir, rule_events)
    except Exception as e:  # noqa: BLE001
        pass  # 落盘失败不阻断推送

    # 2. SSE 推送 (入 pending_alerts 队列)
    if quote_svc:
        sse_alerts = [{
            "source": ev["source"], "type": ev["type"], "rule_id": ev["rule_id"],
            "strategy_id": None, "symbol": ev["symbol"], "name": ev["name"],
            "message": ev["message"], "price": ev["price"], "change_pct": ev["change_pct"],
            "signals": ev["signals"], "severity": ev["severity"],
            "conditions": ev["conditions"], "logic": ev["logic"],
        } for ev in rule_events]
        try:
            quote_svc.push_alerts(sse_alerts)
        except Exception:  # noqa: BLE001
            pass

    # 3. 飞书推送
    if quote_svc:
        try:
            quote_svc._maybe_send_webhook(rule_events, engine)
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": True,
        "triggered": len(rule_events),
        "events": [{"symbol": ev["symbol"], "name": ev["name"], "message": ev["message"]} for ev in rule_events],
    }
