"""F11 盘后定时回跑收藏策略 — job 核心逻辑与偏好存取。

行为契约:
    - 偏好键 backtest_auto_rerun = {enabled, hour, minute, window_days},
      默认 {false, 16, 40, 90}; 开关关闭时 job 入口零开销直接返回。
    - job 逻辑 (run_favorite_reruns): 取 run_store 中 favorite=true 且
      kind='strategy' 的 Run (创建时间倒序, 前 max_runs=10 个), 每个用其
      config 快照重建回测, 区间覆盖为滚动窗 [今天-window_days, 今天],
      其余参数照快照; 经与手动复跑端点 (_rerun_execute) 完全相同的
      service 路径执行, 成功后保存新 Run (label='定时复跑',
      source_run_id=原 run)。单个失败记日志继续, 全部完成记汇总。

可测性: execute (service 路径) 与 store 均可注入, 测试用 tmp_path 隔离,
不跑真实回测、不读行情、不写真实 data/。

安全边界: 只读用户偏好与 backtest_runs 目录; 不接外部 HTTP; 写入仅限
backtest_runs 下的新 Run 文件 (与手动复跑同一落盘入口)。
"""
from __future__ import annotations

import logging
import types
import uuid
from collections.abc import Callable
from datetime import date, timedelta

from app.backtest.run_store import BacktestRun, BacktestRunStore

logger = logging.getLogger(__name__)

# 调度 job id (与偏好键区分: 偏好键描述"用户想要什么", job id 描述"调度器跑什么")
BACKTEST_RERUN_JOB_ID = "backtest_favorite_rerun"
# 偏好键 (preferences.json 内的字段名)
PREF_KEY = "backtest_auto_rerun"
# 新 Run 的固定标签 — 经运行历史既有标签系统展示, 前端无需改动
RERUN_LABEL = "定时复跑"
# 单次 job 最多复跑的收藏 Run 数 (按创建时间倒序取前 N)
MAX_RERUN_RUNS = 10
# 滚动窗口天数范围 (与偏好校验一致)
WINDOW_DAYS_MIN = 30
WINDOW_DAYS_MAX = 365

_SNAPSHOT_CHANGED_WARNING = (
    "rerun_data_snapshot_changed: 复跑时数据快照已变化，与原 run 不可直接比较"
)


# ================================================================
# 偏好存取 — 跟随 app/services/preferences.py 的规范化风格
# (clamp 到合法范围, 损坏值回退默认), 但不修改该模块。
# ================================================================

_BACKTEST_AUTO_RERUN_DEFAULTS = {
    "enabled": False,
    "hour": 16,
    "minute": 40,
    "window_days": 90,
}


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    """偏好文件里的损坏值 (非数值/越界) 回退默认并钳制到 [lo, hi]。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def get_backtest_auto_rerun() -> dict:
    """读取回测定时复跑偏好, 损坏/缺失字段回退默认。"""
    from app.services import preferences

    stored = preferences.load().get(PREF_KEY, {})
    if not isinstance(stored, dict):
        stored = {}
    d = _BACKTEST_AUTO_RERUN_DEFAULTS
    return {
        "enabled": bool(stored.get("enabled", d["enabled"])),
        "hour": _clamp_int(stored.get("hour", d["hour"]), 0, 23, d["hour"]),
        "minute": _clamp_int(stored.get("minute", d["minute"]), 0, 59, d["minute"]),
        "window_days": _clamp_int(
            stored.get("window_days", d["window_days"]),
            WINDOW_DAYS_MIN, WINDOW_DAYS_MAX, d["window_days"],
        ),
    }


def set_backtest_auto_rerun(
    enabled: bool, hour: int, minute: int, window_days: int
) -> dict:
    """保存回测定时复跑偏好 (调用方 API 层已做 422 校验, 这里再钳制兜底)。"""
    from app.services import preferences

    saved = get_backtest_auto_rerun()  # 以规范化结果为准
    saved.update({
        "enabled": bool(enabled),
        "hour": _clamp_int(hour, 0, 23, saved["hour"]),
        "minute": _clamp_int(minute, 0, 59, saved["minute"]),
        "window_days": _clamp_int(window_days, WINDOW_DAYS_MIN, WINDOW_DAYS_MAX, saved["window_days"]),
    })
    preferences.save({PREF_KEY: saved})
    return saved


# ================================================================
# job 核心逻辑 (可测)
# ================================================================

class _RequestShim:
    """最小 request 形态 — 复用 api 层 _rerun_execute 时只提供 .app.state。

    _rerun_execute 只经由 request.app.state 访问 repo / strategy_engine /
    backtest_engine, 不触碰其他 Request 能力, 因此该 shim 足以让其按
    生产路径执行。
    """

    def __init__(self, state) -> None:
        self.app = types.SimpleNamespace(state=state)


def _resolve_state(repo):
    """取真实 app.state (含 strategy_engine 等单例); 启动极早期未注册时
    退化为仅含 repo 的最小 state, 引擎按需创建, 缺失依赖按单 run 失败处理,
    不伪造成功。"""
    from app.jobs.daily_pipeline import _get_app_state

    state = _get_app_state()
    if state is not None and getattr(state, "repo", None) is not None:
        return state
    return types.SimpleNamespace(repo=repo, strategy_engine=None)


def make_default_execute(repo) -> Callable[[BacktestRun], tuple[dict, str]]:
    """构造生产执行路径: 与手动复跑端点共用 _rerun_execute (同一 service 层)。

    state/shim 只构建一次, 使 BacktestEngine 单例在整个 job 内复用
    (与 API 请求共享同一 app.state 上的缓存)。
    """
    from app.api.backtest import _rerun_execute

    shim = _RequestShim(_resolve_state(repo))

    def execute(run: BacktestRun) -> tuple[dict, str]:
        return _rerun_execute(shim, run)

    return execute


def run_favorite_reruns(
    store: BacktestRunStore,
    *,
    execute: Callable[[BacktestRun], tuple[dict, str]],
    today: date | None = None,
    window_days: int = 90,
    max_runs: int = MAX_RERUN_RUNS,
    on_progress=None,
) -> dict:
    """复跑收藏的策略 Run, 写新 Run (label='定时复跑')。

    - execute(run): 注入的执行路径, 接收已替换为滚动窗口 [今天-window_days,
      今天] 的 Run (其余 config 照原快照), 返回 (完整 payload, kind) —
      生产环境为 _rerun_execute 同款 service 路径。
    - store: 注入的运行历史存储 (测试用 tmp_path 隔离)。
    - 单个 run 失败记日志继续下一个; 返回 {total, success, failed, failed_run_ids}。
    """
    from app.api.backtest import _run_from_strategy_payload

    emit = on_progress or (lambda *args, **kwargs: None)
    window_days = max(WINDOW_DAYS_MIN, min(WINDOW_DAYS_MAX, int(window_days)))
    end = today or date.today()
    start = end - timedelta(days=window_days)

    items = store.list_runs(kind="strategy", favorite=True, limit=max_runs).get("items", [])
    total = len(items)
    if total == 0:
        logger.info("backtest_favorite_rerun: 无收藏策略 Run, 直接返回")
        return {"total": 0, "success": 0, "failed": 0, "failed_run_ids": []}

    success = 0
    failed = 0
    failed_run_ids: list[str] = []
    emit("backtest_rerun", 0, f"定时复跑开始: {total} 个收藏策略, 滚动窗口 {start} ~ {end}")

    for i, item in enumerate(items, start=1):
        source_id = str(item.get("run_id") or "")
        try:
            run = store.get(source_id)
            cfg = run.config or {}
            if not cfg.get("strategy_id"):
                raise ValueError("config 缺少 strategy_id, 无法复跑")
            # 滚动窗: 仅覆盖区间, 其余参数照快照
            rolling = run.model_copy(update={"config": {
                **cfg, "start": start.isoformat(), "end": end.isoformat(),
            }})
            payload, kind = execute(rolling)
            new_run = _run_from_strategy_payload(payload, kind=kind)
            # 与手动复跑端点同款: 数据快照变化时显式警告, 不静默
            old_hash = (run.data_snapshot or {}).get("snapshot_hash")
            new_hash = (new_run.data_snapshot or {}).get("snapshot_hash")
            if old_hash and new_hash and old_hash != new_hash:
                new_run.warnings = list(dict.fromkeys(
                    [*new_run.warnings, _SNAPSHOT_CHANGED_WARNING]
                ))
            new_run.source_run_id = run.run_id
            new_run.label = RERUN_LABEL
            new_run.run_id = uuid.uuid4().hex[:16]
            store.save(new_run)
            success += 1
            logger.info("backtest_favorite_rerun: %s → %s", run.run_id, new_run.run_id)
            emit("backtest_rerun", int(i * 100 / total),
                 f"复跑成功 {success}/{total}: {run.run_id} → {new_run.run_id}")
        except Exception as e:  # 单个失败不能中断整批
            failed += 1
            failed_run_ids.append(source_id)
            logger.warning("backtest_favorite_rerun: 复跑 %s 失败: %s", source_id, e)
            emit("backtest_rerun", int(i * 100 / total),
                 f"复跑失败 {failed}/{total}: {source_id} ({e})")

    summary = {"total": total, "success": success, "failed": failed,
               "failed_run_ids": failed_run_ids}
    logger.info("backtest_favorite_rerun 完成: 成功 %d / 失败 %d (共 %d)",
                success, failed, total)
    emit("done", 100, f"定时复跑完成: 成功 {success} / 失败 {failed}")
    return summary


def run_backtest_favorite_rerun_job(repo, *, on_progress=None) -> dict:
    """job 入口 (由调度器在工作线程调用): 读偏好, 关→零开销直接返回。"""
    pref = get_backtest_auto_rerun()
    if not pref["enabled"]:
        return {"skipped": "disabled", "total": 0, "success": 0, "failed": 0,
                "failed_run_ids": []}
    store = BacktestRunStore(repo.store.data_dir)
    return run_favorite_reruns(
        store,
        execute=make_default_execute(repo),
        window_days=pref["window_days"],
        on_progress=on_progress,
    )
