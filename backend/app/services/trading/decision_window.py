"""决策窗口 / 冷却期 / 重复抑制 — 纯函数门禁 (YMOS §决策纪律移植)。

三个独立判定 (均为纯函数, 不落盘, 不创建交易事件, 不修改交易生命周期):

1. 决策窗口是否开放 — :func:`is_window_open`
   对应 YMOS ``cadence.decisionWindow`` / ``execution.allowedDecisionWindows``:
   判断只允许在收盘后 / 指定时间窗内形成。跨日窗口 (start>end) 自动按跨午夜处理。
   盘中形成判断 → 由调用方据 ``allowed=False`` 自行记红旗; 本模块不写任何事实。

2. 冷却期是否结束 — :func:`is_cooldown_satisfied`
   对应 YMOS 纪律门 d2 (``COOL_MIN=10`` 分钟): 开始审这笔到扣扳机的最短间隔。
   冲动发生后能被快速满足的门槛会退化成橡皮图章 —— 这条锁的是"写份文档给已做的
   决定盖章"。``started_at`` 缺失 → 无法确认已冷静 → fail-closed (拒绝)。

3. 重复决策是否被抑制 — :func:`is_duplicate_suppressed`
   对应 YMOS 纪律门 d1 (不是冲动买回): 止损/止盈后日内买回要重走全部建仓条件。
   同一决策键在抑制窗口内的重复 → suppressed, 必须重新走完整流程。

聚合入口 :func:`evaluate_decision_window` 同时跑三项, 输出结构化 reason / next_allowed_at。

时区: 全程 tz-aware。naive 时间按默认时区 (Asia/Shanghai) 本地化并记 warning;
``now`` 缺失取当前时刻。
跨日边界: start>end 的窗口自动按跨午夜处理。
无效配置 → fail-closed (默认拒绝), reason 说明具体字段错误, evidence 记录坏值。

YMOS 红线只能保持或收紧门禁, 不能被用户输入关闭 —— 本模块不提供任何"跳过"旁路:
``coolSkipped`` 那种"跳过会记进决策文件"的逃生门属于 Console 决策台的展示语义,
在纯函数判定层面冷却期不可绕过。
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, timedelta
from typing import Any, Sequence
from zoneinfo import ZoneInfo

__all__ = [
    "DEFAULT_TZ",
    "DEFAULT_COOLDOWN_MINUTES",
    "DEFAULT_SUPPRESSION_MINUTES",
    "is_window_open",
    "is_cooldown_satisfied",
    "is_duplicate_suppressed",
    "next_window_open",
    "evaluate_decision_window",
    "DecisionWindowError",
]

DEFAULT_TZ = "Asia/Shanghai"
# YMOS 买卖决策台 d2 冷却期默认值 (分钟)。
DEFAULT_COOLDOWN_MINUTES = 10
# YMOS d1 冲动买回抑制默认窗口 (分钟): 同一决策键的日内重复默认抑制 60 分钟。
DEFAULT_SUPPRESSION_MINUTES = 60

# next_window_open 向前扫描的最大天数 (防止无限循环 + 容错配置错误)。
_MAX_DAY_SCAN = 8


class DecisionWindowError(ValueError):
    """决策窗口配置无效 (被 :func:`evaluate_decision_window` 转为 fail-closed)。"""


# ── 时区 / 时间工具 ──────────────────────────────────────
def _tz(name: str | None) -> ZoneInfo:
    """解析时区名; 未知名 → 抛 DecisionWindowError (fail-closed 触发条件)。"""
    tz_name = (name or DEFAULT_TZ).strip()
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # KeyError / ValueError / 不支持的 zone
        raise DecisionWindowError(f"未知时区: {tz_name!r}") from exc


def _ensure_aware(dt: datetime | None, tz: ZoneInfo, *, label: str) -> tuple[datetime | None, str | None]:
    """规范化输入时间。

    - ``None`` → ``None`` (缺失时间, 由调用方决定 fail-closed/soft)。
    - naive → 按 ``tz`` 本地化 (记 warning); tz-aware → 原样。
    返回 (规范后的时间 | None, warning | None)。
    """
    if dt is None:
        return None, None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz), f"{label} 为 naive 时间, 按默认时区 {tz} 本地化"
    return dt.astimezone(tz), None


def _now_aware(now: datetime | None, tz: ZoneInfo) -> datetime:
    """``now`` 缺失取当前时刻; naive 本地化到 tz。"""
    if now is None:
        return datetime.now(tz=tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _parse_hhmm(value: Any) -> int | None:
    """``"HH:MM"`` → 午夜起分钟数; 非法 → None。容忍 ``"H:MM"``。"""
    if isinstance(value, str):
        value = value.strip()
    if not isinstance(value, str) or value.count(":") != 1:
        return None
    h_str, m_str = value.split(":")
    if not (h_str.isdigit() and m_str.isdigit()):
        return None
    h, m = int(h_str), int(m_str)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _normalize_min(value: Any) -> int | None:
    """解析分钟数: 接受 ``"HH:MM"`` 字符串或已解析的 int 分钟 (幂等)。

    让 ``_parse_windows`` 对已解析过的内部结构 (int start/end) 二次调用安全,
    避免 evaluate 聚合 → is_window_open / next_window_open 链路上的重复解析丢窗。
    """
    if isinstance(value, bool):
        return None  # bool 是 int 子类, 排除
    if isinstance(value, int):
        return value if 0 <= value <= 1439 else None
    return _parse_hhmm(value)


# ── 窗口解析 / 校验 ──────────────────────────────────────
def _parse_window(raw: Any) -> dict[str, Any] | None:
    """解析单个窗口 dict → 标准化内部结构; 任一字段非法 → None (视为坏配置)。

    幂等: 接受原始 ``"HH:MM"`` 配置, 也接受已解析的 int 分钟内部结构。
    """
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    start = _normalize_min(raw.get("start"))
    end = _normalize_min(raw.get("end"))
    if not name or start is None or end is None:
        return None
    if start == end:
        return None  # 零长度窗口无意义
    return {"name": name, "start": start, "end": end,
            "tz": str(raw.get("tz") or DEFAULT_TZ).strip() or DEFAULT_TZ}


def _parse_windows(windows: Any) -> list[dict[str, Any]]:
    """解析窗口列表, 丢弃坏项。空列表 → [] (语义: 无窗口限制 = 永远开放)。"""
    if windows is None:
        return []
    if not isinstance(windows, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for raw in windows:
        parsed = _parse_window(raw)
        if parsed is not None:
            out.append(parsed)
    return out


def _is_inside_window(now_local_min: int, win: dict[str, Any]) -> bool:
    """``now`` 在窗口 tz 内的分钟数是否落在 [start, end) 内。自动处理跨午夜。"""
    s, e = win["start"], win["end"]
    if s < e:
        return s <= now_local_min < e
    # 跨午夜: start > end → [start, 24:00) ∪ [00:00, end)
    return now_local_min >= s or now_local_min < e


# ── 1. 决策窗口 ──────────────────────────────────────────
def is_window_open(
    windows: Sequence[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
    default_tz: str = DEFAULT_TZ,
) -> dict[str, Any]:
    """判断 ``now`` 是否落在任一允许的决策窗口内。纯函数。

    ``windows`` 为空 / None → 无窗口限制, 永远开放 (``open=True``)。
    返回::

        {
          "open": bool,
          "matched": str | None,        # 命中的窗口名
          "detail": str,
          "next_open_at": str | None,   # 下次开放时刻 ISO; open 时为 None
          "evidence": {"now_local": "ISO", "windows": [...], "warnings": [...]}
        }
    """
    tz = _tz(default_tz)
    now_aware = _now_aware(now, tz)
    parsed = _parse_windows(windows)

    # 无窗口限制
    if not parsed:
        return {
            "open": True,
            "matched": None,
            "detail": "未配置决策窗口 → 无时间限制",
            "next_open_at": None,
            "evidence": {"now_local": _to_iso(now_aware), "windows": [], "warnings": []},
        }

    warnings: list[str] = []
    matched: str | None = None
    for win in parsed:
        wtz = _tz(win["tz"])
        local = now_aware.astimezone(wtz)
        mins = local.hour * 60 + local.minute
        if _is_inside_window(mins, win):
            matched = win["name"]
            break

    if matched is not None:
        return {
            "open": True,
            "matched": matched,
            "detail": f"当前在决策窗口「{matched}」内",
            "next_open_at": None,
            "evidence": {
                "now_local": _to_iso(now_aware),
                "windows": [_window_meta(w) for w in parsed],
                "warnings": warnings,
            },
        }

    nxt = next_window_open(parsed, now_aware)
    return {
        "open": False,
        "matched": None,
        "detail": f"当前不在任何决策窗口内, 下次开放 {_to_iso(nxt)}",
        "next_open_at": _to_iso(nxt),
        "evidence": {
            "now_local": _to_iso(now_aware),
            "windows": [_window_meta(w) for w in parsed],
            "warnings": warnings,
        },
    }


def next_window_open(
    windows: Sequence[dict[str, Any]] | None,
    after: datetime,
    *,
    default_tz: str = DEFAULT_TZ,
) -> datetime | None:
    """最早 ``>= after`` 且落在某窗口内的 tz-aware 时刻。无窗口限制 → ``after`` 本身。

    向前扫描最多 :data:`_MAX_DAY_SCAN` 天。若 ``after`` 已在窗口内 → 返回 ``after``。
    """
    tz = _tz(default_tz)
    if after.tzinfo is None:
        after = after.replace(tzinfo=tz)
    parsed = _parse_windows(windows)
    if not parsed:
        return after

    best: datetime | None = None
    for win in parsed:
        wtz = _tz(win["tz"])
        anchor_date = after.astimezone(wtz).date()
        for day_offset in range(_MAX_DAY_SCAN + 1):
            d = anchor_date + timedelta(days=day_offset)
            open_dt, close_dt = _window_bounds_on(win, wtz, d)
            # after 已在 [open, close) 内
            if open_dt <= after < close_dt:
                candidate: datetime | None = after
            elif after <= open_dt:
                candidate = open_dt
            else:
                continue  # 本日窗口已过
            if best is None or candidate < best:
                best = candidate
    return best


def _window_bounds_on(win: dict[str, Any], wtz: ZoneInfo, d: _date) -> tuple[datetime, datetime]:
    """窗口在日期 ``d`` (tz 内) 的 [open, close) 闭开区间 (tz-aware)。"""
    open_dt = _at(d, win["start"], wtz)
    if win["start"] < win["end"]:
        close_dt = _at(d, win["end"], wtz)
    else:
        # 跨午夜: 收盘在次日
        close_dt = _at(d + timedelta(days=1), win["end"], wtz)
    return open_dt, close_dt


def _at(d: _date, mins: int, tz: ZoneInfo) -> datetime:
    return datetime(d.year, d.month, d.day, mins // 60, mins % 60, tzinfo=tz)


def _window_meta(win: dict[str, Any]) -> dict[str, Any]:
    return {"name": win["name"], "start": _fmt_min(win["start"]),
            "end": _fmt_min(win["end"]), "tz": win["tz"]}


def _fmt_min(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


# ── 2. 冷却期 ────────────────────────────────────────────
def is_cooldown_satisfied(
    started_at: datetime | None,
    cooldown_minutes: float,
    *,
    now: datetime | None = None,
    default_tz: str = DEFAULT_TZ,
) -> dict[str, Any]:
    """判断 ``started_at`` 到 ``now`` 的间隔是否达到冷却期。纯函数。

    - ``cooldown_minutes`` 非正 → fail-closed (拒绝, reason 说明配置非法)。
    - ``started_at`` 缺失 → 无法确认已冷静 → fail-closed (拒绝)。
    返回::

        {
          "satisfied": bool,
          "detail": str,
          "elapsed_minutes": float | None,
          "remaining_minutes": float | None,
          "next_allowed_at": str | None,   # started_at + cooldown (ISO); 满足时 None
          "evidence": {...}
        }
    """
    tz = _tz(default_tz)
    now_aware = _now_aware(now, tz)
    warnings: list[str] = []

    # 非法冷却时长 → fail-closed
    if not isinstance(cooldown_minutes, (int, float)) or cooldown_minutes != cooldown_minutes:  # NaN
        return _cooldown_fail_closed("冷却期配置非法", now_aware, warnings, windows_cfg_invalid=True)
    if cooldown_minutes <= 0:
        return _cooldown_fail_closed(
            f"冷却期必须为正数, 实际 {cooldown_minutes}", now_aware, warnings)

    started, warn = _ensure_aware(started_at, tz, label="started_at")
    if warn:
        warnings.append(warn)
    if started is None:
        return {
            "satisfied": False,
            "detail": "未提供决策开始时间 started_at → 无法确认已过冷却期 (fail-closed)",
            "elapsed_minutes": None,
            "remaining_minutes": None,
            "next_allowed_at": None,
            "evidence": {"now": _to_iso(now_aware), "cooldown_minutes": cooldown_minutes,
                         "warnings": warnings},
        }

    # started 在 now 之后 (时钟回拨 / 跨日错位) → 视为未到冷却期, 记 evidence
    elapsed = (now_aware - started).total_seconds() / 60.0
    clock_back = elapsed < 0
    if clock_back:
        warnings.append("started_at 晚于 now (时钟回拨/跨日错位), 按 fail-closed 处理")
        return {
            "satisfied": False,
            "detail": f"started_at({started.isoformat()}) 晚于 now → 时间错位, 拒绝",
            "elapsed_minutes": elapsed,
            "remaining_minutes": None,
            "next_allowed_at": None,
            "evidence": {"now": _to_iso(now_aware), "started_at": _to_iso(started),
                         "cooldown_minutes": cooldown_minutes, "warnings": warnings,
                         "clock_skew": True},
        }

    remaining = cooldown_minutes - elapsed
    if remaining <= 0:
        return {
            "satisfied": True,
            "detail": f"已冷静 {elapsed:.1f} 分钟 ≥ 冷却期 {cooldown_minutes:g} 分钟",
            "elapsed_minutes": elapsed,
            "remaining_minutes": 0.0,
            "next_allowed_at": None,
            "evidence": {"now": _to_iso(now_aware), "started_at": _to_iso(started),
                         "cooldown_minutes": cooldown_minutes, "warnings": warnings},
        }
    nxt = started + timedelta(minutes=cooldown_minutes)
    return {
        "satisfied": False,
        "detail": f"已冷静 {elapsed:.1f} 分钟, 还差 {remaining:.1f} 分钟 (冷却期 {cooldown_minutes:g})",
        "elapsed_minutes": elapsed,
        "remaining_minutes": remaining,
        "next_allowed_at": _to_iso(nxt),
        "evidence": {"now": _to_iso(now_aware), "started_at": _to_iso(started),
                     "cooldown_minutes": cooldown_minutes, "warnings": warnings},
    }


def _cooldown_fail_closed(reason: str, now_aware: datetime, warnings: list[str],
                          windows_cfg_invalid: bool = False) -> dict[str, Any]:
    return {
        "satisfied": False,
        "detail": f"{reason} → fail-closed (拒绝)",
        "elapsed_minutes": None,
        "remaining_minutes": None,
        "next_allowed_at": None,
        "evidence": {"now": _to_iso(now_aware), "warnings": warnings,
                     "config_invalid": True},
    }


# ── 3. 重复抑制 ──────────────────────────────────────────
def is_duplicate_suppressed(
    decision_key: str,
    recent_decisions: Sequence[dict[str, Any]] | None,
    suppression_minutes: float,
    *,
    now: datetime | None = None,
    default_tz: str = DEFAULT_TZ,
) -> dict[str, Any]:
    """判断同一决策键是否在抑制窗口内被重复提交 → suppressed。纯函数。

    ``recent_decisions``: 每项 ``{"key": str, "at": datetime}`` (at 可缺失/坏值 → 丢弃该项,
    不报错)。
    - ``suppression_minutes`` 非正 → 抑制关闭 (不抑制任何重复)。
    - 同键最近一条在 ``[now - suppression, now]`` 内 → suppressed=True。
    返回::

        {
          "suppressed": bool,
          "matched_key": str | None,
          "matched_at": str | None,
          "detail": str,
          "next_allowed_at": str | None,   # matched_at + suppression (ISO); 未抑制时 None
          "evidence": {...}
        }
    """
    tz = _tz(default_tz)
    now_aware = _now_aware(now, tz)
    warnings: list[str] = []
    key = (str(decision_key or "")).strip()

    # 非法抑制时长 → 不抑制 (fail-soft: 不因配置错误阻塞决策, 但记 warning)
    if not isinstance(suppression_minutes, (int, float)) or suppression_minutes != suppression_minutes:
        warnings.append(f"抑制窗口配置非法 ({suppression_minutes!r}), 关闭重复抑制")
        return _dup_not_suppressed(key, now_aware, warnings, detail="抑制窗口非法 → 不抑制")
    if suppression_minutes <= 0:
        return _dup_not_suppressed(
            key, now_aware, warnings, detail=f"抑制窗口 {suppression_minutes:g} ≤ 0 → 不抑制")

    if not key:
        warnings.append("decision_key 为空, 无法做重复抑制 → 不抑制")
        return _dup_not_suppressed(key, now_aware, warnings, detail="决策键为空 → 不抑制")

    cutoff = now_aware - timedelta(minutes=suppression_minutes)
    matched_at: datetime | None = None
    bad_count = 0
    for item in recent_decisions or []:
        if not isinstance(item, dict):
            bad_count += 1
            continue
        if (item.get("key") or "") != key:
            continue
        at_raw = item.get("at")
        if not isinstance(at_raw, datetime):
            bad_count += 1
            continue
        at, warn = _ensure_aware(at_raw, tz, label="recent_decision.at")
        if warn:
            warnings.append(warn)
        if at is None:
            continue
        # 取窗口内最新的一条
        if at > now_aware:
            continue  # 未来时间不算 (时钟错位保护)
        if at >= cutoff and (matched_at is None or at > matched_at):
            matched_at = at

    if matched_at is None:
        return _dup_not_suppressed(
            key, now_aware, warnings,
            detail=f"决策键「{key}」在最近 {suppression_minutes:g} 分钟内无重复记录")

    nxt = matched_at + timedelta(minutes=suppression_minutes)
    return {
        "suppressed": True,
        "matched_key": key,
        "matched_at": _to_iso(matched_at),
        "detail": (f"决策键「{key}」在 {matched_at.isoformat()} 已提交过, "
                   f"抑制至 {nxt.isoformat()} (窗口 {suppression_minutes:g} 分钟)"),
        "next_allowed_at": _to_iso(nxt),
        "evidence": {"now": _to_iso(now_aware), "decision_key": key,
                     "suppression_minutes": suppression_minutes,
                     "bad_entries": bad_count, "warnings": warnings},
    }


def _dup_not_suppressed(key: str, now_aware: datetime, warnings: list[str], *, detail: str) -> dict[str, Any]:
    return {
        "suppressed": False,
        "matched_key": None,
        "matched_at": None,
        "detail": detail,
        "next_allowed_at": None,
        "evidence": {"now": _to_iso(now_aware), "decision_key": key, "warnings": warnings},
    }


# ── 聚合: evaluate_decision_window ───────────────────────
def evaluate_decision_window(
    config: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    started_at: datetime | None = None,
    decision_key: str | None = None,
    recent_decisions: Sequence[dict[str, Any]] | None = None,
    default_tz: str = DEFAULT_TZ,
) -> dict[str, Any]:
    """同时判定窗口 / 冷却 / 重复抑制, 输出聚合结果。纯函数, 不落盘。

    ``config``::

        {
          "cooldownMinutes": 10,          # 默认 :data:`DEFAULT_COOLDOWN_MINUTES`
          "suppressionMinutes": 60,       # 默认 :data:`DEFAULT_SUPPRESSION_MINUTES`
          "windows": [{"name","start","end","tz"}, ...],  # 空 → 无窗口限制
          "tz": "Asia/Shanghai"           # 默认时区, 覆盖 default_tz
        }

    返回::

        {
          "allowed": bool,                # 窗口开 AND 冷却过 AND 未被抑制
          "reason": str,
          "next_allowed_at": str | None,  # 三项同时满足的最早时刻 ISO
          "checks": {"window": {...}, "cooldown": {...}, "duplicate": {...}},
          "evidence": {...}
        }

    无效配置 (未知时区 / 不可解析字段 / cooldownMinutes 非正 等) → ``allowed=False`` (fail-closed),
    ``reason`` 说明根因, ``evidence.config_invalid=True``。
    """
    warnings: list[str] = []
    cfg = config if isinstance(config, dict) else {}
    cfg_tz = str(cfg.get("tz") or default_tz or DEFAULT_TZ).strip() or DEFAULT_TZ

    # 时区非法 → 整体 fail-closed (后续所有时间计算都依赖它)
    try:
        tz = _tz(cfg_tz)
    except DecisionWindowError as exc:
        return _aggregate_fail_closed(
            f"配置时区无效: {exc}", config,
            {"config_invalid": True, "reason_type": "bad_timezone"})

    now_aware = _now_aware(now, tz)

    # windows: 坏项被 _parse_windows 静默丢弃; 但若原始 windows 非 list → fail-closed
    raw_windows = cfg.get("windows")
    if raw_windows is not None and not isinstance(raw_windows, (list, tuple)):
        return _aggregate_fail_closed(
            f"配置 windows 必须为列表, 实际 {type(raw_windows).__name__}", config,
            {"config_invalid": True, "reason_type": "bad_windows_type"})
    parsed_windows = _parse_windows(raw_windows)

    cooldown_minutes = cfg.get("cooldownMinutes", DEFAULT_COOLDOWN_MINUTES)
    suppression_minutes = cfg.get("suppressionMinutes", DEFAULT_SUPPRESSION_MINUTES)

    # ── 逐项判定 ──
    win_check = is_window_open(parsed_windows, now=now_aware, default_tz=cfg_tz)
    cool_check = is_cooldown_satisfied(started_at, cooldown_minutes, now=now_aware, default_tz=cfg_tz)
    dup_check = is_duplicate_suppressed(
        decision_key or "", recent_decisions, suppression_minutes,
        now=now_aware, default_tz=cfg_tz)

    window_open = bool(win_check["open"])
    cooldown_ok = bool(cool_check["satisfied"])
    duplicate_blocked = bool(dup_check["suppressed"])
    allowed = window_open and cooldown_ok and (not duplicate_blocked)

    checks = {"window": win_check, "cooldown": cool_check, "duplicate": dup_check}

    if allowed:
        return {
            "allowed": True,
            "reason": "决策窗口开放、冷却期已过、无重复抑制 → 允许",
            "next_allowed_at": None,
            "checks": checks,
            "evidence": {"now": _to_iso(now_aware), "config": cfg, "warnings": warnings},
        }

    # ── 计算 next_allowed_at: 三项同时满足的最早时刻 ──
    deadlines: list[datetime] = []
    if not cooldown_ok:
        d = _parse_iso(cool_check.get("next_allowed_at"), tz)
        if d is not None:
            deadlines.append(d)
        else:
            # 冷却因配置非法/缺失时间 fail-closed 且给不出具体截止 → 用 now 作下界
            deadlines.append(now_aware)
    if duplicate_blocked:
        d = _parse_iso(dup_check.get("next_allowed_at"), tz)
        if d is not None:
            deadlines.append(d)
    binding = max(deadlines) if deadlines else now_aware

    if parsed_windows:
        nxt = next_window_open(parsed_windows, binding, default_tz=cfg_tz)
    else:
        nxt = binding if deadlines else None

    # reason: 列出阻塞项
    blockers: list[str] = []
    if not window_open:
        blockers.append(f"决策窗口未开放({win_check['detail']})")
    if not cooldown_ok:
        blockers.append(f"冷却期未过({cool_check['detail']})")
    if duplicate_blocked:
        blockers.append(f"重复决策被抑制({dup_check['detail']})")

    return {
        "allowed": False,
        "reason": "；".join(blockers),
        "next_allowed_at": _to_iso(nxt),
        "checks": checks,
        "evidence": {"now": _to_iso(now_aware), "config": cfg, "warnings": warnings},
    }


def _aggregate_fail_closed(reason: str, config: Any, extra_evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "allowed": False,
        "reason": f"{reason} → fail-closed (拒绝)",
        "next_allowed_at": None,
        "checks": {},
        "evidence": {"config": config, **extra_evidence},
    }


def _parse_iso(iso: str | None, tz: ZoneInfo) -> datetime | None:
    """解析本模块输出的 ISO 字符串回 tz-aware datetime; 失败 → None。"""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None
