"""Signal Scorecard 持久化 — 两条 append-only JSONL 事件流 (events + outcomes)。

镜像 trading/store.py 模式: 写前按确定性 key 去重, threading.Lock 并发安全,
永不清理 (分析数据, 非日志)。损坏行 fail-soft 跳过。

布局:
    data_dir/user_data/signal_scorecard/
        signal_events.jsonl    # 不可变事件 (signal_key, symbol, date)
        signal_outcomes.jsonl  # T+N 前向评估结果

不变量:
    - SignalEvent.id = {signal_key}_{symbol}_{date} (确定性 dedup key)
    - SignalOutcome 唯一键 = (event_id, horizon, engine_version)
    - outcome 一旦写入即不可覆盖 (append-only; 重算需新 engine_version)
    - 前向交易日不足的事件保持 pending, 不写入 unable 行冻结未来

本模块是纯本地分析工具的存储层: 不接 provider、不写交易事件、不生成荐股建议。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

# 评估引擎版本 (重算时 bump, 用作 outcome 幂等键的一部分, 隔离新旧口径)。
ENGINE_VERSION = "tickflow-signal-v1"
# 记分卡评估的 4 个前向交易日 horizon。
HORIZONS: tuple[int, ...] = (1, 3, 5, 10)
# hit/miss/neutral 边界带 (|return%| < band → neutral)。
NEUTRAL_BAND_PCT = 2.0

_lock = threading.Lock()


def scorecard_dir(data_dir: Path) -> Path:
    d = data_dir / "user_data" / "signal_scorecard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def events_path(data_dir: Path) -> Path:
    return scorecard_dir(data_dir) / "signal_events.jsonl"


def outcomes_path(data_dir: Path) -> Path:
    return scorecard_dir(data_dir) / "signal_outcomes.jsonl"


# ── 低层读取 ─────────────────────────────────────────────
def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL, 损坏行 fail-soft 跳过 (不抛异常)。"""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ── 确定性 id ────────────────────────────────────────────
def make_event_id(signal_key: str, symbol: str, date_str: str) -> str:
    """确定性事件 id = {signal_key}_{symbol}_{date}。同一信号同日同标的不重复实例化。"""
    return f"{signal_key}_{symbol}_{date_str}"


def make_outcome_id(event_id: str, horizon: int, engine_version: str = ENGINE_VERSION) -> str:
    """确定性 outcome id = {event_id}_{horizon}_{engine_version}。"""
    return f"{event_id}_{horizon}_{engine_version}"


# ── append-only 写入 (写前去重) ──────────────────────────
def append_event(data_dir: Path, event: dict[str, Any]) -> bool:
    """追加一条信号事件。写前按 id 去重, 已存在则不写。

    返回 True=实际写入, False=已存在 (幂等)。写失败抛 OSError (调用方显式处理)。
    """
    eid = event.get("id")
    if not eid:
        return False
    line = json.dumps(event, ensure_ascii=False)
    with _lock:
        p = events_path(data_dir)
        existing = _read_jsonl(p)
        if any(e.get("id") == eid for e in existing):
            return False
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True


def append_outcome(data_dir: Path, outcome: dict[str, Any]) -> bool:
    """追加一条 outcome。写前按 (event_id, horizon, engine_version) 去重, 已存在则不写。

    返回 True=实际写入, False=已存在 (幂等, 不覆盖 — completed 不可变语义)。
    """
    eid = outcome.get("event_id")
    horizon = outcome.get("horizon")
    if not eid or horizon is None:
        return False
    ev = outcome.get("engine_version", ENGINE_VERSION)
    line = json.dumps(outcome, ensure_ascii=False)
    with _lock:
        p = outcomes_path(data_dir)
        existing = _read_jsonl(p)
        key = (eid, horizon, ev)
        for o in existing:
            if (o.get("event_id"), o.get("horizon"),
                    o.get("engine_version", ENGINE_VERSION)) == key:
                return False
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True


# ── 查询 (内存全量加载 + 过滤) ───────────────────────────
def list_events(
    data_dir: Path,
    signal_key: str | None = None,
    symbol: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """读取全部事件并按字段过滤。status (pending/mature) 由 outcomes 派生。

    返回按 (date, created_ts) 倒序。
    """
    events = _read_jsonl(events_path(data_dir))
    if signal_key is not None:
        events = [e for e in events if e.get("signal_key") == signal_key]
    if symbol is not None:
        events = [e for e in events if e.get("symbol") == symbol]
    if date_from is not None:
        events = [e for e in events if str(e.get("date", "")) >= date_from]
    if date_to is not None:
        events = [e for e in events if str(e.get("date", "")) <= date_to]
    if status is not None:
        outcomes = _read_jsonl(outcomes_path(data_dir))
        events = [e for e in events if e.get("id") is not None
                  and _event_status(e["id"], outcomes) == status]
    events.sort(
        key=lambda e: (str(e.get("date", "")), str(e.get("created_ts", ""))),
        reverse=True,
    )
    if limit is not None and limit >= 0:
        events = events[:limit]
    return events


def list_outcomes(
    data_dir: Path,
    event_id: str | None = None,
    horizon: int | None = None,
    engine_version: str | None = None,
) -> list[dict[str, Any]]:
    """读取全部 outcome 并按字段过滤。"""
    outcomes = _read_jsonl(outcomes_path(data_dir))
    if event_id is not None:
        outcomes = [o for o in outcomes if o.get("event_id") == event_id]
    if horizon is not None:
        outcomes = [o for o in outcomes if o.get("horizon") == horizon]
    if engine_version is not None:
        outcomes = [o for o in outcomes
                    if o.get("engine_version", ENGINE_VERSION) == engine_version]
    return outcomes


# ── 事件级状态派生 ───────────────────────────────────────
def _event_status(event_id: str, outcomes: list[dict[str, Any]]) -> str:
    """事件级状态: pending (任一 horizon 缺 outcome) / mature (全部 horizon 已有 outcome)。

    horizon 已写入 (无论 completed/unable) 即视为该 horizon 终态 — append-only 语义下
    无法覆盖, 故达到 4 个 horizon 行即 mature。
    """
    horizons_done = {
        o.get("horizon") for o in outcomes
        if o.get("event_id") == event_id and o.get("horizon") in HORIZONS
    }
    return "mature" if all(h in horizons_done for h in HORIZONS) else "pending"


def event_status(data_dir: Path, event_id: str) -> str:
    return _event_status(event_id, _read_jsonl(outcomes_path(data_dir)))


def event_outcome_map(
    data_dir: Path, event_ids: list[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """返回 {event_id: [outcome, ...]} 索引, 供聚合/详情用。"""
    outcomes = _read_jsonl(outcomes_path(data_dir))
    out: dict[str, list[dict[str, Any]]] = {}
    for o in outcomes:
        eid = o.get("event_id")
        if eid is None:
            continue
        if event_ids is not None and eid not in set(event_ids):
            continue
        out.setdefault(eid, []).append(o)
    return out
