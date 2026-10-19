"""盘中分钟K增量落盘服务 (全量分钟能力, 按能力路由)。

两段式拉取 (见 feat/minute-strategy 方案):
- 全天修复轮: 全市场日内分时批量并发脉冲一次拉当日全部分钟K — 冷启动
  (如 10 点才开服务, 补 9:30 起缺口) / 覆盖滞后超阈值 / 连续空轮自愈时触发。
- 稳态增量轮: 传标的池/universe 单请求返回全市场每只最新 3 根, 靠
  _write_minute_partition 的 unique(symbol,datetime) 幂等合并滚出全天。

单轮合并写入当日 kline_minute 分区, 供分钟策略 (minute_filter) 读到新鲜数据。

数据源路由 (像其他能力一样可接入):
- 生效源由偏好 full_minute_data_provider 决定 (设置 → 数据源 → 全量分钟):
  TickFlow (Expert 档, intraday.batch + intraday.universe) 或声明 full_minute
  数据集的插件/自定义源。
- 自定义源契约 (docs/plugin-development.md): get_intraday_batch(symbols) 修复轮
  (未实现自动回退 get_minute 当日窗口); get_intraday_latest(count) 稳态增量
  (可选, 未实现自动降级为仅修复轮并放慢节奏至 >=60s)。
- 能力门控 Cap.INTRADAY_UNIVERSE: TickFlow 按档位探测, 自定义源声明数据集且
  被路由时由 policy._augment_custom_sources 补授 — 门控口径对两类源统一。

设计约束:
- 修复轮并发脉冲: TickFlow 路径全市场按 batch_size 分块 (5546/200 = 28 块)
  一次打出。任何 60s 滑动窗口至多一个脉冲 (28 < 48 安全 rpm); 单块失败不拖垮
  整轮, 失败块单独重试一次 (见 fetch_intraday_full_market_burst)。自定义源
  由 provider 自管批量与限速 (rpm 配置/内部并发), 服务不代限。
- 稳态轮单请求: 无脉冲并发, 间隔可低至 3s; 实际节奏 = max(间隔, 单轮完成),
  服务端响应 ~5s 时自动退化为响应节奏, 不会重叠请求。
- 固定节奏: 默认 6s 一轮 (clamp [3, 300]), 不补跑 (missed 轮次直接跳过);
  仅修复轮的自定义源下限抬到 60s (全天批量打不住 6s 节奏)。
- 仅连续竞价时段运行 (9:30-11:30 / 13:00-15:00), 午休/收盘自动暂停与恢复;
  午休后恢复因覆盖滞后会多跑一次修复轮, 幂等无害。
- 不与其他分钟能力冲突: 与 盘后分钟同步 (kline.minute.batch) / 分时监控路径
  分属不同限流池; 落盘走 _write_minute_partition 的 unique(symbol,datetime)
  合并, 与盘后同步写同一分区安全幂等。

分层: 本模块只做调度/落盘/状态; 取数全部经 kline_sync 边界层
(TickFlow: fetch_intraday_full_market_burst / fetch_intraday_universe_increment;
自定义: fetch_intraday_custom_batch / fetch_intraday_custom_latest),
保持插件化边界不泄漏。
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from app.market_time import cn_now, cn_today, in_continuous_session
from app.services import preferences

logger = logging.getLogger(__name__)

# 轮询间隔允许范围 (秒): 稳态轮单请求无并发脉冲, 下限 3s;
# 上限 120s — universe 端点每标的只回最新 3 根, 间隔超过 3 分钟必留缺口,
# 每轮都会触发修复轮, 稳态设计失效, 故不允许配到 120s 以上。
REFRESH_INTERVAL_MIN = 3
REFRESH_INTERVAL_MAX = 120
# 等待步长 (秒): 循环小步睡眠, 便于快速停止与偏好热生效。
_LOOP_STEP_S = 2.0
# 当日覆盖滞后超过该分钟数 (≈ universe 单请求 3 根余量) → 触发全天修复轮。
_REPAIR_LAG_MINUTES = 3.0
# 连续空轮达到该次数 → 强制全天修复轮 (自愈 universe 端点持续异常)。
_EMPTY_ROUNDS_TO_REPAIR = 2
# 仅修复轮的自定义源 (无 get_intraday_latest 廉价增量): 全天批量节奏下限,
# 6s 一轮全市场拉取会把绝大多数源打爆。
_REPAIR_ONLY_MIN_INTERVAL_S = 60


def _in_continuous_session(now=None) -> bool:
    """A股连续竞价时段 (北京时间): 9:30-11:30 / 13:00-15:00, 仅工作日。"""
    return in_continuous_session(now)


@dataclass
class _RefreshState:
    """服务运行状态 (status() 的内存镜像, 循环线程内更新)。"""

    rounds: int = 0
    last_round_at: float | None = None      # epoch 秒
    last_round_ms: float | None = None      # 单轮耗时
    last_rows: int = 0                      # 上轮写入行数 (合并后)
    last_symbols: int = 0                   # 上轮覆盖标的数
    last_requests: int = 0                  # 上轮请求数 (增量恒 1, 修复=分块数+重试)
    last_mode: str | None = None            # 上轮模式: "increment" / "full"
    last_error: str | None = None
    next_round_at: float | None = None      # epoch 秒
    extra: dict[str, Any] = field(default_factory=dict)


class MinuteRefreshService:
    """盘中分钟增量刷新: 单实例挂 app.state.minute_refresh, 后台守护线程。"""

    def __init__(self, repo) -> None:
        self._repo = repo
        self._app_state: Any | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = _RefreshState()
        self._round_lock = threading.Lock()  # 同时只允许一轮 (手动触发与定时轮互斥)
        self._empty_rounds = 0               # 连续空轮计数 (escalate 到全天修复)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def set_repo(self, repo) -> None:
        self._repo = repo

    def set_app_state(self, app_state: Any) -> None:
        self._app_state = app_state

    def start(self) -> bool:
        """启动后台线程 (幂等)。开关/时段/能力判断都在循环内每轮做, 热生效。"""
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="minute-refresh", daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # 门控
    # ------------------------------------------------------------------

    def capability_ok(self) -> bool:
        """Cap.INTRADAY_UNIVERSE (全量分钟) 存在。能力探测结果缓存在 app.state。

        门控挂在稳态增量的主能力上; TickFlow 路径全天修复轮用的 intraday.batch
        与其同属 Expert 档 (tiers.yaml), 两者必然同时持有。自定义源声明
        full_minute 数据集且被路由时, 由 policy._augment_custom_sources 补授
        同一能力键 — 门控口径对两类源统一。
        """
        capset = getattr(self._app_state, "capabilities", None) if self._app_state else None
        if capset is None:
            return False
        try:
            from app.tickflow.capabilities import Cap

            return capset.has(Cap.INTRADAY_UNIVERSE)
        except Exception:
            return False

    def active_provider(self) -> str:
        """全量分钟生效源名；失效的自定义源不得静默伪装成 TickFlow。"""
        try:
            saved = str(preferences.load().get("full_minute_data_provider", "tickflow") or "tickflow").lower()
            selected = preferences.get_full_minute_data_provider()
            if saved != "tickflow" and selected == "tickflow":
                return f"unavailable:{saved}"
            return selected
        except Exception:  # noqa: BLE001
            return "unavailable:unknown"

    def _resolve_custom(self) -> tuple[object | None, str]:
        """解析自定义源。返回 (provider_or_None, effective_name):

        - 明确偏好 tickflow → (None, "tickflow")
        - 自定义源失效或缺少 full_minute → (None, "unavailable:<name>")
        - 成功 → (provider, name)
        """
        from app.services import kline_sync

        name = self.active_provider()
        if name == "tickflow":
            return (None, "tickflow")
        if name.startswith("unavailable:"):
            logger.warning("full_minute provider %s unavailable; not falling back to TickFlow",
                           name.removeprefix("unavailable:"))
            return (None, name)
        provider, _use_tickflow, err = kline_sync._resolve_full_minute_provider(name)
        if err is not None or provider is None:
            logger.warning(
                "full_minute provider %s unavailable; not falling back to TickFlow: %s",
                name, err,
            )
            return (None, f"unavailable:{name}")
        return (provider, name)

    def _custom_supports_increment(self, provider: object) -> bool:
        """自定义源是否实现 get_intraday_latest (稳态增量); 否则仅修复轮。"""
        return callable(getattr(provider, "get_intraday_latest", None))

    def repair_only(self) -> bool:
        """当前生效源只能全天修复轮 (无廉价增量端点) — 节奏下限抬到 60s。"""
        provider, _name = self._resolve_custom()
        return provider is not None and not self._custom_supports_increment(provider)

    def _effective_interval(self) -> int:
        """本轮间隔: 偏好值; 仅修复轮的自定义源下限 60s (全天批量打不住 6s 节奏)。"""
        interval = preferences.get_minute_refresh_interval()
        if self.repair_only():
            return max(interval, _REPAIR_ONLY_MIN_INTERVAL_S)
        return interval

    def _gate_reason(self) -> str | None:
        """返回本轮不执行的原因 (None = 放行)。"""
        if not preferences.get_minute_refresh_enabled():
            return "disabled"
        if not self.capability_ok():
            return "capability"
        if not _in_continuous_session():
            return "outside_trading_hours"
        # 节假日 (工作日但休市): 周几门控覆盖不到, 由交易日探针剔除。
        # 未知 (None) 维持现状 — 空轮升级机制兜底 (探针失灵时的第二道防线)。
        from app.services import trading_day

        if trading_day.is_trading_day() is False:
            return "holiday"
        return None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                reason = self._gate_reason()
                if reason is None:
                    interval = self._effective_interval()
                    started = time.time()
                    self._run_round()
                    # 固定节奏: 下一轮 = max(本轮起点+间隔, 本轮完成), 不补跑
                    finish = time.time()
                    self._state.next_round_at = max(started + interval, finish)
                    # 等到下一轮 (小步睡眠保持可停/偏好热切换)
                    while not self._stop.is_set():
                        now = time.time()
                        gate = self._gate_reason()
                        if gate is not None:
                            self._state.next_round_at = None
                            break  # 门控关闭 → 回外层等待重评估
                        if now >= self._state.next_round_at:
                            break
                        self._stop.wait(min(_LOOP_STEP_S, max(0.0, self._state.next_round_at - now)))
                    continue
            except Exception as e:
                self._state.last_error = f"round failed: {e}"
                logger.warning("全量分钟轮次异常: %s", e)
            self._stop.wait(_LOOP_STEP_S)

    # ------------------------------------------------------------------
    # 单轮
    # ------------------------------------------------------------------

    def _today_coverage_lag_minutes(self) -> float | None:
        """当日分区最新K距现在的分钟数; None = 当日无数据。

        覆盖度探测只读当日分区文件 (毫秒级); 任何异常按无数据处理 →
        本轮走全天修复, 不会因探测失败而丢增量。
        """
        with contextlib.suppress(Exception):
            part = (
                self._repo.store.data_dir / "kline_minute"
                / f"date={cn_today().isoformat()}" / "part.parquet"
            )
            if not part.exists():
                return None
            mx = pl.read_parquet(part, columns=["datetime"])["datetime"].max()
            if mx is None:
                return None
            # 分区 datetime 为北京墙钟 naive, cn_now 带时区 → 剥齐再比
            return (cn_now().replace(tzinfo=None) - mx).total_seconds() / 60
        return None

    def _select_mode(self) -> str:
        """选轮次模式: 稳态增量 (universe 单请求) vs 全天修复 (burst 脉冲)。

        当日已有数据且覆盖滞后 ≤ _REPAIR_LAG_MINUTES (≈ 3 根余量) → 增量;
        冷启动 / 断档超阈值 / 连续空轮 → 全天修复。
        """
        if self._empty_rounds >= _EMPTY_ROUNDS_TO_REPAIR:
            return "full"
        lag = self._today_coverage_lag_minutes()
        if lag is None or lag > _REPAIR_LAG_MINUTES:
            return "full"
        return "increment"

    def _run_round(self) -> None:
        from app.services import kline_sync

        t0 = time.perf_counter()
        custom, provider_name = self._resolve_custom()
        if provider_name.startswith("unavailable:"):
            self._state.last_error = f"full_minute provider unavailable: {provider_name.removeprefix('unavailable:')}"
            self._state.last_requests = 0
            self._state.last_symbols = 0
            return
        mode = self._select_mode()
        if mode == "increment" and custom is not None and not self._custom_supports_increment(custom):
            # 无廉价增量端点的源: 增量轮退化为修复轮 (全天批量幂等覆盖, 数据不丢)
            mode = "full"
        mode_label = "增量" if mode == "increment" else "全天修复"
        with self._round_lock:
            if mode == "increment":
                # fetch 计时只覆盖网络取数; full 分支的 universe 维表读取不计入
                fetch_started = time.perf_counter()
                if custom is not None:
                    latest = kline_sync.fetch_intraday_custom_latest(custom, provider_name)
                    df, requests = latest if latest is not None else (pl.DataFrame(), 0)
                else:
                    df, requests = kline_sync.fetch_intraday_universe_increment()
                self._state.last_symbols = (
                    df["symbol"].n_unique() if not df.is_empty() else 0
                )
            else:
                symbols = self._universe()
                self._state.last_symbols = len(symbols)
                if not symbols:
                    self._state.last_error = "empty universe (instruments 未加载)"
                    logger.warning("全量分钟[%s] 本轮中止: 标的池为空 (instruments 未加载)", mode_label)
                    return
                fetch_started = time.perf_counter()
                if custom is not None:
                    df, requests = kline_sync.fetch_intraday_custom_batch(
                        custom, provider_name, symbols,
                    )
                else:
                    capset = getattr(self._app_state, "capabilities", None) if self._app_state else None
                    df, requests = kline_sync.fetch_intraday_full_market_burst(symbols, capset)
            fetch_ms = (time.perf_counter() - fetch_started) * 1000
            self._state.last_requests = requests
            if df.is_empty():
                self._empty_rounds += 1
                self._state.last_error = f"intraday {mode} returned no data"
                logger.warning(
                    "全量分钟[%s] 本轮返回空数据: %d 请求, 取数 %.0fms",
                    mode_label, requests, fetch_ms,
                )
                return
            self._empty_rounds = 0
            write_started = time.perf_counter()
            written = kline_sync._write_minute_partition(
                df, self._repo.store.data_dir / "kline_minute",
            )
            write_ms = (time.perf_counter() - write_started) * 1000

        self._state.rounds += 1
        self._state.last_round_at = time.time()
        self._state.last_round_ms = (time.perf_counter() - t0) * 1000
        self._state.last_rows = written
        self._state.last_mode = mode
        self._state.last_error = None
        logger.info(
            "全量分钟[%s] 第 %d 轮: 取数 %.0fms (%d 请求, %d 标的), "
            "落盘 %.0fms (%d 行), 总计 %.0fms",
            mode_label, self._state.rounds, fetch_ms, self._state.last_requests,
            self._state.last_symbols, write_ms, written, self._state.last_round_ms,
        )

    def _universe(self) -> list[str]:
        """全市场 A 股标的 (instruments 维表, 与盘后分钟同步同一来源)。"""
        inst = self._repo.get_instruments()
        if inst.is_empty() or "symbol" not in inst.columns:
            return []
        return inst["symbol"].cast(pl.Utf8).unique().sort().to_list()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def is_healthy(self) -> bool:
        """读侧 freshness 判定: 本地 kline_minute 分区是否正被服务持续写入。

        条件: 偏好开启 + 轮询线程存活 + 最近一轮距现在 ≤ max(2*间隔, 30s)。
        健康时消费方 (自选分时等) 可信任本地分区、跳过批量补拉;
        连续失败轮不更新 last_round_at → 自动超时判不健康, 无需单独盯 last_error。
        """
        try:
            if not preferences.get_minute_refresh_enabled():
                return False
        except Exception:  # noqa: BLE001 — 偏好文件异常按不健康处理
            return False
        if self._thread is None or not self._thread.is_alive():
            return False
        last = self._state.last_round_at
        if last is None:
            return False
        interval = preferences.get_minute_refresh_interval()
        return (time.time() - float(last)) <= max(2.0 * interval, 30.0)

    def status(self) -> dict[str, Any]:
        import contextlib

        with contextlib.suppress(Exception):
            enabled = preferences.get_minute_refresh_enabled()
        running = self._thread is not None and self._thread.is_alive()
        gate = self._gate_reason()
        return {
            "enabled": enabled,
            "running": running,
            "healthy": self.is_healthy(),
            "provider": self.active_provider(),
            "provider_effective": self._resolve_custom()[1],
            "repair_only": self.repair_only(),
            "interval_seconds": preferences.get_minute_refresh_interval(),
            "capability_ok": self.capability_ok(),
            "in_trading_hours": _in_continuous_session(),
            "gate_reason": gate if (enabled and running) else (gate or "disabled"),
            "rounds": self._state.rounds,
            "last_round_at": self._state.last_round_at,
            "last_round_ms": self._state.last_round_ms,
            "last_rows": self._state.last_rows,
            "last_symbols": self._state.last_symbols,
            "last_requests": self._state.last_requests,
            "last_mode": self._state.last_mode,
            "next_round_at": self._state.next_round_at,
            "last_error": self._state.last_error,
        }

    def trigger_manual_round(self) -> dict[str, Any]:
        """手动触发一轮 (无视时段门控, 但仍受能力门控); 供状态页「立即刷新」。"""
        if not self.capability_ok():
            return {"ok": False, "reason": self._gate_reason() or "capability"}
        threading.Thread(target=self._run_round, daemon=True, name="minute-refresh-manual").start()
        return {"ok": True}
