"""工具目录 — 把因子 / 数据源 / 策略元数据统一序列化为 OpenAI tools 格式。

让 LLM 通过工具按需检索目录 (而非把全部因子塞进 system prompt 撑爆上下文);
run_backtest 复用现有 StrategyBacktestConfig + worker, 只读、受控窗口, 只回传
精简 stats 关键键 (不把 equity_curve / trades 塞给 LLM)。

设计边界:
  - 本模块只做「序列化 + 分发」, 不持有策略引擎 / 数据目录单例, 依赖由调用方注入。
  - run_backtest 是重任务 (子进程), 由 execute_tool 用 asyncio.to_thread 挪出事件循环。
  - Codex CLI 无 tools= 协议, 相关门控在 api 层入口 fail-closed, 不在本模块降级。
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# 工具名常量 (与 build_tool_schemas 里 function.name 一一对应)
LIST_FACTORS = "list_factors"
LIST_STRATEGIES = "list_strategies"
LIST_DATA_CAPABILITIES = "list_data_capabilities"
RUN_BACKTEST = "run_backtest"

# run_backtest 回传给 LLM 的 stats 键白名单 (控制 token, 不塞曲线/成交明细)
_BACKTEST_STATS_KEYS: frozenset[str] = frozenset({
    "total_return",
    "annual_return",
    "max_drawdown",
    "sharpe",
    "sortino",
    "calmar",
    "win_rate",
    "profit_factor",
    "n_trades",
    "avg_pnl",
})

# 回测默认窗口 (对齐 api/backtest.py 的 FACTOR_DEFAULT_DAYS, 受控窗口防超大区间)
_DEFAULT_BACKTEST_DAYS = 180


def _function_schema(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def build_tool_schemas() -> list[dict[str, Any]]:
    """组装 4 个工具的 OpenAI tools 格式 JSON Schema (纯静态, 无运行时状态)。"""
    return [
        _function_schema(
            LIST_FACTORS,
            "检索可用因子目录 (约 100 个)。返回因子 id、中文名、分组、公式说明、"
            "依赖列与适用资产, 用于挑选合法的评分字段与信号依赖。可按 asset_type "
            "(stock/etf) 过滤, stable_only=true 时只返回稳定因子。",
            {
                "type": "object",
                "properties": {
                    "asset_type": {
                        "type": "string",
                        "enum": ["stock", "etf"],
                        "description": "按适用资产过滤; 缺省返回全部",
                    },
                    "stable_only": {
                        "type": "boolean",
                        "description": "只返回稳定因子 (排除实验/废弃)",
                    },
                },
                "required": [],
            },
        ),
        _function_schema(
            LIST_STRATEGIES,
            "检索已加载的策略目录 (含 research_only 研究模板)。返回策略 id、名称、"
            "描述、标签、适用资产/周期、可调参数与执行后端, 用于参考已有策略或路由。",
            {
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        _function_schema(
            LIST_DATA_CAPABILITIES,
            "检索可用的数据源能力目录 (日K/除权/实时/分钟/五档/财务/全量分钟等)。"
            "返回能力 id、中文名、说明与 TickFlow 档位要求, 用于判断可用数据范围。",
            {
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        _function_schema(
            RUN_BACKTEST,
            "对已保存到 data/strategies/ 的策略 (含 ai_ 草稿) 跑一次全量回测, "
            "返回夏普/回撤/胜率/盈亏比等精简指标。只读操作, 不修改任何策略文件。",
            {
                "type": "object",
                "properties": {
                    "strategy_id": {
                        "type": "string",
                        "description": "要回测的策略 id",
                    },
                    "params": {
                        "type": "object",
                        "description": "可选的参数覆盖 (键为参数 id, 值为数值/布尔)",
                    },
                    "start": {
                        "type": "string",
                        "description": "开始日期 YYYY-MM-DD; 缺省=最近 180 天",
                    },
                    "end": {
                        "type": "string",
                        "description": "结束日期 YYYY-MM-DD; 缺省=今天",
                    },
                    "asset_type": {
                        "type": "string",
                        "enum": ["stock", "etf"],
                        "description": "资产类型; 缺省 stock",
                    },
                },
                "required": ["strategy_id"],
            },
        ),
    ]


def list_factors(asset_type: str | None = None, stable_only: bool = False) -> dict[str, Any]:
    """序列化因子目录: factors/registry.py 的 FactorSpec → 精简 dict 列表。"""
    from app.factors.registry import all_factors

    factors = [
        {
            "id": spec.id,
            "label": spec.label,
            "group": spec.group,
            "formula_text": spec.formula_text,
            "kind": spec.kind,
            "dependencies": sorted(spec.dependencies),
            "asset_types": sorted(spec.asset_types),
            "stability": spec.stability,
            "pit": spec.pit,
        }
        for spec in all_factors(asset_type=asset_type, stable_only=stable_only)
    ]
    return {"factors": factors}


def list_strategies(engine) -> dict[str, Any]:
    """序列化策略目录: StrategyEngine.list_strategies(include_research=True) 精简视图。"""
    strategies = []
    for meta in engine.list_strategies(include_research=True):
        strategies.append({
            "id": meta.get("id"),
            "name": meta.get("name", ""),
            "description": meta.get("description", ""),
            "tags": meta.get("tags", []),
            "asset_types": meta.get("asset_types", ["stock"]),
            "timeframes": meta.get("timeframes", ["1d"]),
            "execution_backend": meta.get("execution_backend"),
            "source": meta.get("source"),
            "params": [
                {
                    "id": p.get("id"),
                    "label": p.get("label", ""),
                    "type": p.get("type", ""),
                    "default": p.get("default"),
                }
                for p in meta.get("params", [])
            ],
        })
    return {"strategies": strategies}


def list_data_capabilities() -> dict[str, Any]:
    """序列化数据源能力目录: capabilities.py 的 CAPABILITY_REGISTRY 精简视图。"""
    from app.data_providers.capabilities import CAPABILITY_REGISTRY

    capabilities = [
        {
            "id": cap["id"],
            "label": cap["label"],
            "desc": cap["desc"],
            "tf_tier": cap["tf_tier"],
        }
        for cap in CAPABILITY_REGISTRY
    ]
    return {"capabilities": capabilities}


def run_backtest(
    data_dir: str | Path,
    *,
    strategy_id: str,
    params: dict[str, Any] | None = None,
    start: str | None = None,
    end: str | None = None,
    asset_type: str = "stock",
) -> dict[str, Any]:
    """回测工具桥: 复用 StrategyBacktestConfig + make_worker_task/run_worker_task。

    同步阻塞 (spawn 子进程), 由 execute_tool 用 asyncio.to_thread 调用。
    返回 {"strategy_id", "start", "end", "stats": 精简白名单键}。
    """
    from app.backtest.strategy import StrategyBacktestConfig
    from app.backtest.worker import make_worker_task, run_worker_task
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    end_date = date.fromisoformat(end) if end else date.today()
    start_date = (
        date.fromisoformat(start) if start else end_date - timedelta(days=_DEFAULT_BACKTEST_DAYS)
    )

    cfg = StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=None,
        start=start_date,
        end=end_date,
        params=params,
        asset_type=asset_type,
        # 其余成本/撮合口径走默认 (与 api/backtest.py 的 /strategy/run 默认一致)
    )
    # 与其它重回测端点一致: 走共享重任务限流 (容量 2), 防并发迭代/手动回测叠加挤爆内存
    with shared_heavy_job_limiter.slot("normal"):
        task = make_worker_task("backtest", Path(data_dir), cfg)
        result = run_worker_task(task)

    error = result.get("error")
    if error:
        raise ValueError(f"回测失败: {error}")
    stats = result.get("stats") or {}
    return {
        "strategy_id": strategy_id,
        "start": str(start_date),
        "end": str(end_date),
        "stats": {key: stats[key] for key in _BACKTEST_STATS_KEYS if key in stats},
    }


async def execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    engine=None,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """按 name 分发执行, 返回 {"ok": bool, "result": ... | "error": str}。

    engine / data_dir 由 api 层注入 (request.app.state); list_factors /
    list_data_capabilities 不需要二者, 传 None 亦可。
    """
    try:
        if name == LIST_FACTORS:
            result = list_factors(
                asset_type=args.get("asset_type"),
                stable_only=bool(args.get("stable_only", False)),
            )
        elif name == LIST_STRATEGIES:
            if engine is None:
                return {"ok": False, "error": "策略引擎未注入"}
            result = list_strategies(engine)
        elif name == LIST_DATA_CAPABILITIES:
            result = list_data_capabilities()
        elif name == RUN_BACKTEST:
            if data_dir is None:
                return {"ok": False, "error": "数据目录未注入"}
            strategy_id = str(args.get("strategy_id") or "").strip()
            if not strategy_id:
                return {"ok": False, "error": "run_backtest 缺少 strategy_id"}
            if engine is not None and not engine.has(strategy_id):
                return {"ok": False, "error": f"策略 {strategy_id} 不存在"}
            result = await asyncio.to_thread(
                run_backtest,
                data_dir,
                strategy_id=strategy_id,
                params=args.get("params"),
                start=args.get("start"),
                end=args.get("end"),
                asset_type=args.get("asset_type") or "stock",
            )
        else:
            return {"ok": False, "error": f"未知工具: {name}"}
        return {"ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001 — 工具执行错误统一回填给 LLM, 不打断循环
        return {"ok": False, "error": str(exc) or type(exc).__name__}
