"""Agent 研究工具 — 条件选股股票池 + 后台池回测任务。

设计契约(仅研究用途, 不触碰 trading API / 订单 / 真实成交):

  - 股票池是服务端不可变 artifact: pool_id 为规范化内容的 sha256 前缀,
    同一查询幂等覆写同一文件; 完整 symbols 只落盘, 绝不进入工具返回
    (preview 最多 10 条)。
  - 回测在 daemon thread 中执行, 复用 StrategyBacktestService /
    FactorBacktestService 与共享 BacktestEngine(不依赖 api.backtest 私有
    函数); 任务表锁保护, 完成态 TTL 清理。job_id 由规范化 config hash
    决定 → 同 config 去重。
  - 所有用户输入走 Pydantic v2 ``extra="forbid"`` 封闭模型 + Literal 枚举,
    不开放 SQL / 路径 / 任意表达式; pool_id、job_id 均有 regex 白名单。
  - 回测结果 result.error 非空 → 任务终态 error("cancelled" 单独标记),
    不写 RunCard。成功时经 ResearchStore.save_run_card 写不可变研究卡,
    config 携带 pool_id / pool_as_of / pool_hash / data_watermark 证据,
    stats 与 config 均带"仅用于研究、非交易建议"警告; 返回给 Agent 的
    只有 stats 摘要、run_id 与 run_card 引用, 不含净值曲线和交易明细。

模块导入无副作用: app.* 依赖全部在函数体内延迟导入。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

logger = logging.getLogger(__name__)

# ── 封闭标识符白名单(防路径穿越/未知输入) ───────────────────────
_POOL_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_JOB_ID_RE = re.compile(r"^pb-[0-9a-f]{12}$")
_SYMBOL_RE = re.compile(r"^[0-9A-Z]{1,8}\.(SH|SZ|BJ|HK|INDEX|ETF)$")
# 错误消息里可能带绝对路径(异常链), 落到 Agent 上下文前先打码。
_PATH_IN_MSG_RE = re.compile(r"[/\\][^\s\"']*")

MAX_BACKTEST_DAYS = 186
PREVIEW_LIMIT = 10
POOL_SCHEMA_VERSION = 1
RESEARCH_ONLY_DISCLAIMER = "仅用于研究，非交易建议"
_JOB_TTL_SECONDS = 300.0
_MAX_JOBS = 50

# stats 只放摘要: 从回测结果中白名单挑标量, 大对象(净值/逐笔)留在服务端。
_STRATEGY_STATS_KEYS = (
    "total_return", "annual_return", "max_drawdown", "sharpe", "calmar",
    "win_rate", "profit_factor", "n_trades", "avg_pnl", "avg_win", "avg_loss",
)
_FACTOR_SCALAR_KEYS = ("ic_mean", "ic_std", "ir", "ic_win_rate", "n_symbols", "n_dates")
_LONG_SHORT_KEYS = ("total_return", "annual_return", "max_drawdown", "sharpe", "win_rate")


# ── 封闭输入模型(extra="forbid", 无自由文本表达式) ────────────────
class _ScreenArgs(BaseModel):
    """screen_stock_pool 顶层参数; conditions/order_by 的强类型校验
    委托给 ScreenerQueryRequest(QueryCondition/QueryOrder 同样 forbid)。"""

    model_config = ConfigDict(extra="forbid")

    conditions: list[dict[str, Any]] = Field(min_length=1, max_length=20)
    as_of: date | None = None
    order_by: dict[str, Any] | None = None
    limit: StrictInt = Field(default=100, ge=1, le=500)


class _StartArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool_id: str
    target: Literal["strategy", "factor"]
    start: date
    end: date
    # strategy 专属(strategy 需要 strategy_id)
    strategy_id: str | None = Field(default=None, max_length=128)
    matching: Literal["close_t", "open_t+1"] | None = None
    entry_fill: Literal["close_t", "open_t+1"] | None = None
    exit_fill: Literal["close_t", "open_t+1"] | None = None
    max_positions: StrictInt | None = Field(default=None, ge=1, le=500)
    # factor 专属(factor 需要 factor_name)
    factor_name: str | None = Field(default=None, max_length=128)
    n_groups: StrictInt | None = Field(default=None, ge=2, le=20)
    rebalance: Literal["daily", "weekly", "monthly"] | None = None
    weight: Literal["equal", "factor_weight"] | None = None
    # 两者共有
    fees_pct: float | None = Field(default=None, ge=0, le=0.1)
    slippage_bps: float | None = Field(default=None, ge=0, le=1000)

    @model_validator(mode="after")
    def _check_target_fields(self) -> _StartArgs:
        strategy_only = ("matching", "entry_fill", "exit_fill", "max_positions")
        factor_only = ("n_groups", "rebalance", "weight")
        if self.target == "strategy":
            if not (self.strategy_id or "").strip():
                raise ValueError("target=strategy 需要 strategy_id")
            if self.factor_name is not None:
                raise ValueError("target=strategy 不接受 factor_name")
            stray = [k for k in factor_only if getattr(self, k) is not None]
            if stray:
                raise ValueError(f"因子专属参数不允许用于 target=strategy: {','.join(stray)}")
        else:
            if not (self.factor_name or "").strip():
                raise ValueError("target=factor 需要 factor_name")
            if self.strategy_id is not None:
                raise ValueError("target=factor 不接受 strategy_id")
            stray = [k for k in strategy_only if getattr(self, k) is not None]
            if stray:
                raise ValueError(f"策略专属参数不允许用于 target=factor: {','.join(stray)}")
        return self


class _GetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    wait_seconds: float = Field(default=0, ge=0, le=30)


# ── 任务表(进程内, 锁保护 + 完成态 TTL) ─────────────────────────
_JOBS: dict[str, dict[str, Any]] = {}
_POOL_WRITE_LOCK = threading.Lock()
_JOBS_LOCK = threading.Lock()


# ── 小工具 ────────────────────────────────────────────────────
def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash16(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()[:16]


def _require_repo(app_state: Any):
    repo = getattr(app_state, "repo", None)
    if repo is None:
        raise ValueError("tool requires app_state.repo")
    return repo


def _data_dir(app_state: Any) -> Path:
    return Path(_require_repo(app_state).store.data_dir)


def _pools_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / "agent_pools"


def _data_watermark(repo: Any) -> dict[str, Any]:
    """记录股票池生成时刻的数据水位, 供回测研究卡携带证据。"""
    from app.json_safe import json_safe

    watermark: dict[str, Any] = {"created_at": _utc_now_iso()}
    generation = getattr(repo, "cache_generation", None)
    if isinstance(generation, int):
        watermark["cache_generation"] = generation
    ceiling = getattr(repo, "enriched_read_ceiling", None)
    if ceiling is not None and hasattr(ceiling, "isoformat"):
        watermark["enriched_read_ceiling"] = ceiling.isoformat()
    return json_safe(watermark)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """同目录临时文件 + rename 的原子 JSON 写(参照 storage.atomic_write 惯例)。"""
    from app.json_safe import json_safe

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(json_safe(payload), fh, ensure_ascii=False, indent=2, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _validate_pool(pool: Any, pool_id: str) -> dict[str, Any]:
    """防御性校验池 artifact 结构(文件可能被外部改动)。"""
    if not isinstance(pool, dict):
        raise ValueError(f"unusable pool: {pool_id}")
    if pool.get("pool_id") != pool_id:
        raise ValueError(f"pool_id mismatch in artifact: {pool_id}")
    try:
        date.fromisoformat(str(pool.get("as_of")))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unusable pool as_of: {pool_id}") from exc
    symbols = pool.get("symbols")
    if (
        not isinstance(symbols, list)
        or not symbols
        or len(symbols) > 500
        or not all(isinstance(s, str) and _SYMBOL_RE.fullmatch(s) for s in symbols)
    ):
        raise ValueError(f"unusable pool symbols: {pool_id}")
    conditions = pool.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise ValueError(f"unusable pool conditions: {pool_id}")
    if not isinstance(pool.get("data_watermark"), dict):
        raise ValueError(f"unusable pool watermark: {pool_id}")
    identity = {
        key: pool.get(key)
        for key in ("schema_version", "as_of", "conditions", "order_by", "symbols", "total")
    }
    if _hash16(identity) != pool_id:
        raise ValueError(f"pool checksum mismatch: {pool_id}")
    return pool


def _load_pool(app_state: Any, pool_id: str) -> dict[str, Any]:
    """regex 白名单加载池 artifact, 阻断路径穿越与未知输入。"""
    if not isinstance(pool_id, str) or not _POOL_ID_RE.fullmatch(pool_id):
        raise ValueError("invalid pool_id")
    pools_dir = _pools_dir(_data_dir(app_state)).resolve()
    path = pools_dir / f"{pool_id}.json"
    if path.parent != pools_dir:  # 纵深防御: resolve 后必须仍落在池目录
        raise ValueError("invalid pool_id")
    if not path.is_file():
        raise ValueError(f"unknown pool_id: {pool_id}")
    try:
        pool = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"unreadable pool: {pool_id}") from exc
    return _validate_pool(pool, pool_id)


def _sanitize_error(exc: BaseException) -> str:
    message = str(exc) or type(exc).__name__
    return _PATH_IN_MSG_RE.sub("<path>", message)


# ── 公开工具 1: 条件选股 → 不可变池 artifact ─────────────────────
def screen_stock_pool(app_state: Any, args: dict) -> dict:
    """强类型条件选股并保存服务端股票池; 返回 pool_id/计数/预览, 不返回全量 symbols。"""
    from app.services.screener_query import (
        QueryService,
        ScreenerDataUnavailableError,
        ScreenerQueryRequest,
        ScreenerSemanticError,
        validate_query,
    )

    parsed = _ScreenArgs.model_validate(args or {})
    # 复用现有 typed {field,op,value} 封闭校验(每层 extra="forbid")。
    req = ScreenerQueryRequest.model_validate(
        {
            "conditions": parsed.conditions,
            "as_of": parsed.as_of,
            "order_by": parsed.order_by,
            "limit": parsed.limit,
        }
    )
    applied, order = validate_query(req)
    repo = _require_repo(app_state)
    try:
        result = QueryService(repo).query(req)
    except ScreenerDataUnavailableError as exc:
        raise ValueError(f"筛选数据不可用(字段: {','.join(exc.fields)})") from exc
    except ScreenerSemanticError as exc:
        raise ValueError(f"无效条件 {exc.location}: {exc.reason}") from exc

    rows = result.get("rows") or []
    symbols = [str(row["symbol"]) for row in rows if row.get("symbol")]
    if not symbols:
        raise ValueError("筛选结果为空, 无法构建股票池")
    as_of = str(result["as_of"])
    total = int(result.get("total") or len(symbols))

    content = {
        "schema_version": POOL_SCHEMA_VERSION,
        "as_of": as_of,
        "conditions": json.loads(_canonical(applied)),
        "order_by": {"field": order.field, "direction": order.direction},
        "symbols": symbols,
        "total": total,
    }
    pool_id = _hash16(content)
    pool_path = _pools_dir(_data_dir(app_state)) / f"{pool_id}.json"
    with _POOL_WRITE_LOCK:
        if pool_path.is_file():
            existing = _load_pool(app_state, pool_id)
            if any(existing.get(key) != value for key, value in content.items()):
                raise ValueError(f"pool hash collision: {pool_id}")
            pool = existing
        else:
            pool = {"pool_id": pool_id, **content, "data_watermark": _data_watermark(repo)}
            _atomic_write_json(pool_path, pool)

    preview = [
        {"symbol": row.get("symbol"), "name": row.get("name")}
        for row in rows[:PREVIEW_LIMIT]
        if row.get("symbol")
    ]
    return {
        "status": "success",
        "summary": f"股票池已保存: {total} 只候选(取前 {len(symbols)} 只), as_of={as_of}",
        "pool_id": pool_id,
        "count": len(symbols),
        "total": total,
        "as_of": as_of,
        "preview": preview,
        "next_actions": ["start_pool_backtest"],
        "artifacts": [
            {
                "kind": "agent_pool",
                "pool_id": pool_id,
                "as_of": as_of,
                "count": len(symbols),
                "location": f"user_data/agent_pools/{pool_id}.json",
            }
        ],
    }


# ── 公开工具 2: 启动池回测后台任务 ──────────────────────────────
def start_pool_backtest(app_state: Any, args: dict) -> dict:
    """对已保存股票池启动策略/因子回测; 校验时间约束后返回 job_id。"""
    parsed = _StartArgs.model_validate(args or {})
    pool = _load_pool(app_state, parsed.pool_id)
    pool_as_of = date.fromisoformat(str(pool["as_of"]))
    if parsed.start < pool_as_of:
        raise ValueError(
            f"回测 start({parsed.start.isoformat()}) 不得早于股票池 as_of({pool_as_of.isoformat()})"
        )
    if parsed.end < parsed.start:
        raise ValueError("回测 end 不得早于 start")
    if (parsed.end - parsed.start).days + 1 > MAX_BACKTEST_DAYS:
        raise ValueError(f"回测区间不得超过 {MAX_BACKTEST_DAYS} 个自然日")
    if parsed.target == "strategy" and parsed.start == pool_as_of:
        effective_entry = parsed.entry_fill or parsed.matching or "open_t+1"
        if effective_entry == "close_t":
            raise ValueError("股票池形成当日不得使用 close_t 建仓, 请改用 open_t+1")
    symbols = pool["symbols"]
    if not symbols:
        raise ValueError("股票池为空")

    config: dict[str, Any] = {
        "pool_id": parsed.pool_id,
        "target": parsed.target,
        "start": parsed.start.isoformat(),
        "end": parsed.end.isoformat(),
        "fees_pct": parsed.fees_pct,
        "slippage_bps": parsed.slippage_bps,
    }
    if parsed.target == "strategy":
        config.update(
            strategy_id=parsed.strategy_id,
            matching=parsed.matching,
            entry_fill=parsed.entry_fill,
            exit_fill=parsed.exit_fill,
            max_positions=parsed.max_positions,
        )
    else:
        config.update(
            factor_name=parsed.factor_name,
            n_groups=parsed.n_groups,
            rebalance=parsed.rebalance,
            weight=parsed.weight,
        )
    config = {k: v for k, v in config.items() if v is not None}

    # job_id = 规范化 config hash → 同 config 天然去重。
    job_id = f"pb-{_hash16(config)[:12]}"
    _cleanup_stale_jobs()
    with _JOBS_LOCK:
        existing = _JOBS.get(job_id)
        if existing is not None:
            response = _job_snapshot(existing)
            response["deduplicated"] = True
            return response
        if len(_JOBS) >= _MAX_JOBS:
            raise ValueError("后台回测任务已达上限, 请等待已有任务完成")
        record: dict[str, Any] = {
            "job_id": job_id,
            "config": config,
            "status": "pending",
            "created_at": _utc_now_iso(),
            "created_ts": time.monotonic(),
            "started_ts": None,
            "finished_ts": None,
            "run_id": None,
            "stats": None,
            "run_card_ref": None,
            "error": None,
            "done": threading.Event(),
        }
        _JOBS[job_id] = record

    thread = threading.Thread(
        target=_run_job,
        args=(record, app_state, pool),
        name=f"pool-bt-{job_id}",
        daemon=True,
    )
    try:
        thread.start()
    except Exception as exc:  # noqa: BLE001 — 启动失败不能遗留永久 pending 槽位
        with _JOBS_LOCK:
            if _JOBS.get(job_id) is record:
                _JOBS.pop(job_id, None)
        raise ValueError("无法启动后台回测任务") from exc
    with _JOBS_LOCK:
        return _job_snapshot(record)


# ── 公开工具 3: 查询池回测任务(可短暂等待) ────────────────────────
def get_pool_backtest(app_state: Any, args: dict) -> dict:
    """查询回测任务; pending 返回进度, success 返回 run_id/stats 摘要/run_card 引用。"""
    parsed = _GetArgs.model_validate(args or {})
    if not _JOB_ID_RE.fullmatch(parsed.job_id):
        raise ValueError("invalid job_id")
    _cleanup_stale_jobs()
    with _JOBS_LOCK:
        record = _JOBS.get(parsed.job_id)
    if record is None:
        raise ValueError(f"unknown job_id: {parsed.job_id}")
    if parsed.wait_seconds > 0 and record["status"] in {"pending", "running"}:
        record["done"].wait(parsed.wait_seconds)
    with _JOBS_LOCK:
        return _job_snapshot(record)


# ── 后台执行 ──────────────────────────────────────────────────
def _run_job(record: dict[str, Any], app_state: Any, pool: dict[str, Any]) -> None:
    """daemon thread 主体: 复用现有回测 service, 完成后写不可变研究卡。"""
    try:
        with _JOBS_LOCK:
            if record["status"] != "pending":
                return
            record["status"] = "running"
            record["started_ts"] = time.monotonic()
        config = dict(record["config"])
        result = _execute_backtest(app_state, config, pool["symbols"])
        error = getattr(result, "error", None)
        if error:
            # 回测 service 以 result.error 报告失败(含取消): 明确终态, 不写 RunCard。
            status = "cancelled" if str(error) == "cancelled" else "error"
            _finish_job(record, status=status, error=_sanitize_error(RuntimeError(str(error))))
            return
        run_id = str(getattr(result, "run_id", "") or uuid.uuid4().hex[:10])
        stats = _stats_summary(config["target"], result)
        run_card_ref = _save_pool_run_card(app_state, run_id, config, pool, result, stats)
        _finish_job(record, status="success", run_id=run_id, stats=stats, run_card_ref=run_card_ref)
    except BaseException as exc:  # noqa: BLE001 — 后台线程必须显式落 error 态
        logger.warning("pool backtest job %s failed: %s", record["job_id"], exc)
        _finish_job(record, status="error", error=_sanitize_error(exc))


def _finish_job(record: dict[str, Any], status: str, **fields: Any) -> None:
    with _JOBS_LOCK:
        record["status"] = status
        record["finished_ts"] = time.monotonic()
        for key, value in fields.items():
            record[key] = value
    record["done"].set()


def _execute_backtest(app_state: Any, config: dict[str, Any], symbols: list[str]):
    if config["target"] == "strategy":
        return _execute_strategy_backtest(app_state, config, symbols)
    return _execute_factor_backtest(app_state, config, symbols)


def _shared_engine(app_state: Any):
    """优先复用 app_state.backtest_engine(PanelCache 跨任务生效), 否则按需创建。"""
    engine = getattr(app_state, "backtest_engine", None)
    if engine is None:
        from app.backtest.engine import BacktestEngine

        engine = BacktestEngine(_require_repo(app_state))
        try:
            app_state.backtest_engine = engine
        except (AttributeError, TypeError):  # 只读 state 对象: 各任务自建引擎
            pass
    return engine


def _execute_strategy_backtest(app_state: Any, config: dict[str, Any], symbols: list[str]):
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

    strategy_engine = getattr(app_state, "strategy_engine", None)
    if strategy_engine is None:
        raise ValueError("tool requires app_state.strategy_engine")
    kwargs: dict[str, Any] = {
        "strategy_id": config["strategy_id"],
        "symbols": list(symbols),
        "start": date.fromisoformat(config["start"]),
        "end": date.fromisoformat(config["end"]),
    }
    for key in ("matching", "entry_fill", "exit_fill", "fees_pct", "slippage_bps", "max_positions"):
        if config.get(key) is not None:
            kwargs[key] = config[key]
    service = StrategyBacktestService(_shared_engine(app_state), strategy_engine)
    return service.run(StrategyBacktestConfig(**kwargs))


def _execute_factor_backtest(app_state: Any, config: dict[str, Any], symbols: list[str]):
    from app.backtest.factor import FactorBacktestService, FactorConfig

    kwargs: dict[str, Any] = {
        "factor_name": config["factor_name"],
        "symbols": list(symbols),
        "start": date.fromisoformat(config["start"]),
        "end": date.fromisoformat(config["end"]),
    }
    for key in ("n_groups", "rebalance", "weight", "fees_pct", "slippage_bps"):
        if config.get(key) is not None:
            kwargs[key] = config[key]
    service = FactorBacktestService(_shared_engine(app_state))
    return service.run(FactorConfig(**kwargs))


def _stats_summary(target: str, result: Any) -> dict[str, Any]:
    """只提取白名单标量摘要; 净值曲线/交易明细绝不进入任务表或工具返回。"""
    from app.json_safe import json_safe

    summary: dict[str, Any] = {"disclaimer": RESEARCH_ONLY_DISCLAIMER}
    if target == "factor":
        for key in _FACTOR_SCALAR_KEYS:
            value = getattr(result, key, None)
            if value is not None:
                summary[key] = value
        long_short = getattr(result, "long_short_stats", None)
        if isinstance(long_short, dict):
            for key in _LONG_SHORT_KEYS:
                if long_short.get(key) is not None:
                    summary[f"long_short_{key}"] = long_short[key]
        summary["n_groups"] = len(getattr(result, "group_stats", None) or [])
    else:
        stats = getattr(result, "stats", None)
        if isinstance(stats, dict):
            for key in _STRATEGY_STATS_KEYS:
                if stats.get(key) is not None:
                    summary[key] = stats[key]
    return json_safe(summary)


def _save_pool_run_card(
    app_state: Any,
    run_id: str,
    config: dict[str, Any],
    pool: dict[str, Any],
    result: Any,
    stats: dict[str, Any],
) -> dict[str, Any]:
    """完成时写不可变 RunCard; config 携带池证据与研究用途声明, symbols 以 pool 引用替代。"""
    from app.json_safe import json_safe
    from app.services.research_registry import ResearchStore

    result_config = dict(getattr(result, "config", None) or {})
    result_config.pop("symbols", None)  # 完整池以不可变 pool artifact 为准
    card_config = json_safe(
        {
            **result_config,
            "pool_id": pool["pool_id"],
            "pool_as_of": pool["as_of"],
            "pool_hash": pool["pool_id"],
            "pool_symbols_count": len(pool["symbols"]),
            "data_watermark": pool.get("data_watermark") or {},
            "requested": dict(config),
            "disclaimer": RESEARCH_ONLY_DISCLAIMER,
        }
    )
    kind = "pool_backtest_strategy" if config["target"] == "strategy" else "pool_backtest_factor"
    strategy_def = getattr(result, "strategy_info", None)
    card = ResearchStore(_data_dir(app_state)).save_run_card(
        run_id=run_id,
        kind=kind,
        config=card_config,
        stats=stats,
        strategy_def=strategy_def if isinstance(strategy_def, dict) else None,
    )
    return {
        "run_id": run_id,
        "kind": kind,
        "config_hash": card.config_hash,
        "location": f"research/run_cards/{run_id}.json",
    }


def _job_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    """任务对外快照: 小而封闭, 不含 symbols / 净线 / 交易明细。"""
    config = record["config"]
    status = record["status"]
    base: dict[str, Any] = {
        "job_id": record["job_id"],
        "status": status,
        "target": config.get("target"),
        "pool_id": config.get("pool_id"),
    }
    if status in {"pending", "running"}:
        started = record["started_ts"] or record["created_ts"]
        stage = "执行中" if status == "running" else "排队中"
        base["progress"] = {
            "stage": status,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "start": config.get("start"),
            "end": config.get("end"),
        }
        base["summary"] = f"回测任务{stage}: {config.get('target')} {config.get('start')}~{config.get('end')}"
        base["next_actions"] = ["get_pool_backtest"]
        base["artifacts"] = []
    elif status == "success":
        base["run_id"] = record["run_id"]
        base["stats"] = record["stats"]
        base["run_card_ref"] = record["run_card_ref"]
        base["disclaimer"] = RESEARCH_ONLY_DISCLAIMER
        base["summary"] = f"回测完成: run_id={record['run_id']}({RESEARCH_ONLY_DISCLAIMER})"
        base["next_actions"] = []
        base["artifacts"] = [
            {"kind": "run_card", **(record["run_card_ref"] or {})},
            {"kind": "agent_pool", "pool_id": config.get("pool_id")},
        ]
    else:  # error / cancelled
        base["error"] = record["error"] or status
        base["summary"] = f"回测任务{status}: {base['error']}"
        base["next_actions"] = ["start_pool_backtest"]
        base["artifacts"] = []
    return base


def _cleanup_stale_jobs() -> None:
    """清理过期完成态；容量不足时只淘汰完成任务，绝不丢弃执行中任务。"""
    now = time.monotonic()
    with _JOBS_LOCK:
        stale = [
            job_id
            for job_id, record in _JOBS.items()
            if record["status"] in {"success", "error", "cancelled"}
            and record["finished_ts"] is not None
            and now - record["finished_ts"] > _JOB_TTL_SECONDS
        ]
        for job_id in stale:
            _JOBS.pop(job_id, None)
        if len(_JOBS) >= _MAX_JOBS:
            terminal = sorted(
                (
                    job_id
                    for job_id, record in _JOBS.items()
                    if record["status"] in {"success", "error", "cancelled"}
                ),
                key=lambda job_id: _JOBS[job_id]["finished_ts"] or float("inf"),
            )
            for job_id in terminal[: len(_JOBS) - _MAX_JOBS + 1]:
                _JOBS.pop(job_id, None)


__all__ = [
    "MAX_BACKTEST_DAYS",
    "PREVIEW_LIMIT",
    "RESEARCH_ONLY_DISCLAIMER",
    "get_pool_backtest",
    "screen_stock_pool",
    "start_pool_backtest",
]
