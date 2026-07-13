"""五档盘口 sealed(真假涨停/跌停) 服务 — 独立旁路线。

架构(完全解耦):
  - 只读 enriched(拿涨跌停名单), 不写回 enriched(14列不动)
  - sealed 存独立 parquet(data/depth5/date=xxx/part.parquet)
  - limit_ladder API 查询时 LEFT JOIN(同 ext_columns 机制)
  - signal_limit_up 永远是"价格涨停", sealed 是叠加的真假判定层

数据流:
  盘中轮询线程(交易时段, 独立 sleep, 不绑行情轮询):
    读 enriched 内存缓存(线程安全) → 涨跌停名单 → tf.depth.batch
    → 算 sealed → 更新内存缓存(不落盘) → sealed_ready=True
  盘后定版 job(可配置时间, 默认15:02):
    最后拉一次 → 落盘 depth5 parquet(定版)

三层防护节流("设过大设上限, 设过小设最小值"):
  ① 套餐范围 clamp: Pro 10~120s, Expert 3~120s
  ② 限速安全 clamp: safe = 60/((rpm*0.8)/batches), 涨跌停多就自动放慢
  ③ 系统接管通知: 用户设置会超限时, 推 toast 告知已自动调整
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import date, time as dt_time

from app.market_time import cn_now, cn_today
from pathlib import Path

import polars as pl

from app.tickflow.capabilities import Cap
from app.tickflow.rate_limits import chunked, resolve_limit, sleep_between_batches

logger = logging.getLogger(__name__)


# 套餐 → (轮询间隔下限s, 上限s)
TIER_INTERVAL_RANGE: dict[str, tuple[float, float]] = {
    "pro": (10.0, 120.0),
    "expert": (3.0, 120.0),
}
# 兜底: 其他有 DEPTH5_BATCH 的套餐按 pro 范围
DEFAULT_RANGE = (10.0, 120.0)

# 限速余量: 只用 rpm 的 80%, 给系统其他 depth 调用留空间
RPM_MARGIN = 0.8
# 间隔硬下限/上限(任何套餐)
INTERVAL_HARD_MIN = 10.0
INTERVAL_HARD_MAX = 300.0


class DepthService:
    """五档盘口 sealed 服务 — 单例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 拉取+定版串行锁 (镜像 quote_service._fetch_lock): _fetch_and_seal 可能同时被
        # 请求线程 (run_once persist=True)、轮询线程、盘后 finalize 触发, 都写同一 parquet,
        # 无锁会交叉写坏文件。_lock 只护内存缓存, 此锁护整段 fetch+seal。
        self._fetch_lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._repo = None              # 延迟注入(KlineRepository)
        self._app_state = None         # 延迟注入(FastAPI app.state)

        # 内存缓存: {symbol: SealedEntry}
        # SealedEntry = {sealed_up, sealed_down, ask1_vol, bid1_vol, status, fetched_ts}
        self._sealed_cache: dict[str, dict] = {}
        self._sealed_ready = False
        self._sealed_date: date | None = None     # sealed 数据对应的交易日(可能是昨天,如休市)
        self._sealed_fetched_ts: float = 0.0   # 上次拉取的 perf_counter
        self._sealed_fetched_at: float = 0.0   # 上次拉取的 wall-clock 时间戳
        self._persisted_date: date | None = None  # 已落盘的日期

        # 系统接管状态(防通知刷屏)
        self._last_taken_over: bool | None = None
        self._last_user_interval: float | None = None

    # ================================================================
    # 注入
    # ================================================================

    def set_repo(self, repo) -> None:
        self._repo = repo

    def set_app_state(self, app_state) -> None:
        self._app_state = app_state

    # ================================================================
    # 生命周期
    # ================================================================

    def boot_check(self) -> None:
        """启动补跑: 当天 depth5 文件不存在则 finalize 一次; 已存在则恢复内存缓存。"""
        if not self._has_capability():
            logger.info("depth sealed: 无 DEPTH5_BATCH 能力, 跳过启动补跑")
            return
        today = cn_today()
        if self._persisted_for_date(today):
            # parquet 已存在: 恢复内存缓存(避免重启后每次查询都读 parquet)
            self._restore_from_parquet(today)
            return
        logger.info("depth sealed: 启动补跑今天定版")
        try:
            self.finalize()
        except Exception as e:  # noqa: BLE001
            logger.warning("depth sealed 启动补跑失败: %s", e)

    def _restore_from_parquet(self, d: date) -> None:
        """从 parquet 恢复内存缓存(服务重启后)。"""
        if not self._repo:
            return
        out = self._repo.store.data_dir / "depth5" / f"date={d.isoformat()}" / "part.parquet"
        if not out.exists():
            return
        try:
            df = pl.read_parquet(out)
            cache: dict[str, dict] = {}
            for row in df.to_dicts():
                sym = row.get("symbol")
                if not sym:
                    continue
                cache[sym] = {
                    "sealed_up": row.get("sealed_up"),
                    "sealed_down": row.get("sealed_down"),
                    "ask1_vol": row.get("ask1_vol"),
                    "bid1_vol": row.get("bid1_vol"),
                    "status": row.get("status"),
                    "fetched_ts": row.get("fetched_at"),
                }
            with self._lock:
                self._sealed_cache = cache
                self._sealed_ready = True
                self._sealed_date = d
                self._persisted_date = d
            logger.info("depth sealed: 从 parquet 恢复 %d 只 (日期=%s)", len(cache), d)
        except Exception as e:  # noqa: BLE001
            logger.warning("depth sealed 从 parquet 恢复失败: %s", e)

    def start_polling(self) -> None:
        """启动盘中轮询线程(连板梯队监控开启 + 有能力 + 交易时段)。"""
        if not self._has_capability():
            return
        from app.services import preferences
        if not preferences.get_limit_ladder_monitor_enabled():
            return
        # check-then-act 加锁: 两个线程同时 start_polling 不会各起一个轮询线程
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("depth sealed 盘中轮询已启动")

    def stop_polling(self) -> None:
        """停止盘中轮询线程。"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        logger.info("depth sealed 盘中轮询已停止")

    def apply_monitor_toggle(self, enabled: bool) -> None:
        """连板梯队监控开关切换时调用: 开启→启动轮询, 关闭→停止轮询。"""
        if enabled:
            self.start_polling()
        else:
            self.stop_polling()

    def run_once(self) -> dict:
        """手动触发一次修正(立即拉取 depth + 更新内存缓存)。

        不受监控开关限制 — 用户可随时手动修正一次。
        返回 {"ok": bool, "count": int, "msg": str}
        """
        if not self._has_capability():
            return {"ok": False, "count": 0, "msg": "无五档盘口能力(需 Pro+)"}
        try:
            self._fetch_and_seal(persist=True)  # 落盘, 刷新页面不丢
            with self._lock:
                count = len(self._sealed_cache)
            return {"ok": True, "count": count, "msg": f"已修正 {count} 只"}
        except Exception as e:  # noqa: BLE001
            logger.warning("depth run_once 失败: %s", e)
            return {"ok": False, "count": 0, "msg": f"修正失败: {e}"}

    # ================================================================
    # 核心拉取
    # ================================================================

    def _fetch_and_seal(self, persist: bool = False) -> None:
        """拉一次 depth.batch, 算 sealed, 更新内存缓存(可选落盘)。

        persist=True: 盘后定版, 写 depth5 parquet
        persist=False: 盘中轮询, 只更新内存缓存

        全程持 _fetch_lock: 请求线程 (run_once)、轮询线程、finalize 不会交叉写 parquet。
        """
        with self._fetch_lock:
            self._fetch_and_seal_locked(persist)

    def _fetch_and_seal_locked(self, persist: bool = False) -> None:
        """_fetch_and_seal 的实际逻辑, 须在持有 _fetch_lock 时调用。"""
        if not self._repo:
            return

        # 只读 enriched 内存缓存(线程安全, 避免和 quote_service 写盘竞态)
        enriched, enriched_date = self._repo.get_enriched_latest()
        if enriched.is_empty():
            return

        # 筛涨跌停名单(用 fill_null 防止列缺失)
        syms_up: list[str] = []
        syms_down: list[str] = []
        if "signal_limit_up" in enriched.columns:
            syms_up = enriched.filter(
                pl.col("signal_limit_up").fill_null(False)
            )["symbol"].to_list()
        if "signal_limit_down" in enriched.columns:
            syms_down = enriched.filter(
                pl.col("signal_limit_down").fill_null(False)
            )["symbol"].to_list()

        all_syms = list(dict.fromkeys(syms_up + syms_down))  # 去重保序
        if not all_syms:
            logger.debug("depth sealed: 当日无涨跌停股, 跳过")
            return

        # 拉 depth(涨跌停一次拉, 按 capset batch 切片)
        depth_data = self._call_depth_batch(all_syms)
        if not depth_data:
            logger.warning("depth sealed: depth.batch 返回空")
            return

        up_set = set(syms_up)
        down_set = set(syms_down)
        now_perf = time.perf_counter()
        now_wall = time.time()

        new_cache: dict[str, dict] = {}
        for sym, d in depth_data.items():
            ask_vols = d.get("ask_volumes") or []
            bid_vols = d.get("bid_volumes") or []
            ask1 = ask_vols[0] if ask_vols else None
            bid1 = bid_vols[0] if bid_vols else None
            # depth 返回的 timestamp(毫秒 epoch), 回退到当前 wall-clock
            depth_ts = d.get("timestamp")
            fetched = (depth_ts / 1000.0) if isinstance(depth_ts, (int, float)) and depth_ts else now_wall
            entry = {
                # 涨停真封: 涨停价上卖一(主动卖压)为 0
                "sealed_up": (ask1 == 0) if sym in up_set and ask1 is not None else None,
                # 跌停真封: 跌停价上买一为 0
                "sealed_down": (bid1 == 0) if sym in down_set and bid1 is not None else None,
                "ask1_vol": ask1,
                "bid1_vol": bid1,
                "status": "limit_down" if sym in down_set and sym not in up_set else "limit_up",
                "fetched_ts": fetched,
            }
            new_cache[sym] = entry

        with self._lock:
            self._sealed_cache = new_cache
            self._sealed_ready = True
            self._sealed_date = enriched_date  # 记录数据对应的交易日(可能是昨天,如休市)
            self._sealed_fetched_ts = now_perf
            self._sealed_fetched_at = now_wall

        logger.info("depth sealed: 拉取 %d 只 (涨停%d/跌停%d) 日期=%s%s",
                    len(new_cache), len(syms_up), len(syms_down),
                    enriched_date, " → 落盘" if persist else "")

        # 缓存已更新: 通知 SSE 推 depth_updated, 触发连板梯队刷新封单数据。
        self._notify_depth_updated(len(new_cache))

        if persist and enriched_date:
            self._persist(enriched_date)

    def _call_depth_batch(self, symbols: list[str]) -> dict:
        """调 tf.depth.batch, 按 capset 的 batch 切片 + 节流。返回 {symbol: MarketDepth}。"""
        from app.tickflow.client import get_client
        tf = get_client()

        capset = self._get_capset()
        limit = resolve_limit(capset, Cap.DEPTH5_BATCH, default_batch=100, default_rpm=30)

        result: dict = {}
        chunks = chunked(symbols, limit.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, limit.rpm, default_interval=2.0)
            try:
                # SDK 的 batch 内部已按 batch_size 切, 这里再切一层防单请求过大
                data = tf.depth.batch(chunk)
                if isinstance(data, dict):
                    result.update(data)
            except Exception as e:  # noqa: BLE001
                logger.warning("depth.batch 第 %d 批失败(%d 只): %s", i + 1, len(chunk), e)
                # 单批失败不影响其他批
        return result

    def finalize(self) -> None:
        """盘后定版: 拉一次 + 落盘。"""
        if not self._has_capability():
            return
        self._fetch_and_seal(persist=True)

    # ================================================================
    # 落盘
    # ================================================================

    def _persist(self, today: date) -> None:
        """把内存缓存写 depth5/date=今天/part.parquet。"""
        with self._lock:
            cache = dict(self._sealed_cache)
        if not cache:
            return

        rows = []
        for sym, e in cache.items():
            rows.append({
                "symbol": sym,
                "sealed_up": e.get("sealed_up"),
                "sealed_down": e.get("sealed_down"),
                "ask1_vol": e.get("ask1_vol"),
                "bid1_vol": e.get("bid1_vol"),
                "status": e.get("status"),
                "fetched_at": e.get("fetched_ts"),
            })
        # 显式 schema: sealed_up/sealed_down 是 bool 与 None 混合, 不指定 schema
        # polars 会按首行推断类型, 后续遇到不一致 (bool vs null) 报
        # "could not append value: false of type: bool to the builder"。
        df = pl.DataFrame(rows, schema={
            "symbol": pl.Utf8,
            "sealed_up": pl.Boolean,
            "sealed_down": pl.Boolean,
            "ask1_vol": pl.Int64,
            "bid1_vol": pl.Int64,
            "status": pl.Utf8,
            "fetched_at": pl.Float64,
        })
        ds = today.isoformat()
        out = self._repo.store.data_dir / "depth5" / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        # 原子写: 先写临时文件再 os.replace, 避免读侧 (get_sealed_map) 读到半写 parquet
        tmp = out.with_name(out.name + ".tmp")
        df.write_parquet(tmp)
        os.replace(tmp, out)
        self._persisted_date = today
        logger.info("depth sealed 落盘: %d 行 → %s", df.height, out)

    def _persisted_for_date(self, d: date) -> bool:
        """检查某日 depth5 文件是否已存在。"""
        if not self._repo:
            return False
        out = self._repo.store.data_dir / "depth5" / f"date={d.isoformat()}" / "part.parquet"
        return out.exists()

    # ================================================================
    # 查询(供 limit_ladder API 用)
    # ================================================================

    def get_sealed_map(self, target_date: date, is_down: bool) -> dict:
        """返回 {symbol: {sealed, vol, ready, age}} 供 JOIN。

        优先内存缓存(盘中), 回退 parquet(历史/盘后)。
        sealed: bool | None (None=待确认或降级)
        vol: 封单量(int) | None
        ready: sealed 数据是否就绪(False→降级标识)
        age: 距上次拉取秒数(盘后定版为 None)
        """
        # 内存缓存(sealed 数据对应的交易日 = target_date 时才用)
        if self._sealed_date and target_date == self._sealed_date and self._sealed_ready and self._sealed_cache:
            return self._read_from_memory(is_down)
        # parquet(历史或盘后定版)
        return self._read_from_parquet(target_date, is_down)

    def _read_from_memory(self, is_down: bool) -> dict:
        sealed_key = "sealed_down" if is_down else "sealed_up"
        # 封单量: 涨停=买一量(涨停价买单堆积), 跌停=卖一量(跌停价卖单堆积)
        vol_key = "ask1_vol" if is_down else "bid1_vol"
        now = time.perf_counter()
        with self._lock:
            cache = dict(self._sealed_cache)
            fetched_ts = self._sealed_fetched_ts
        age = (now - fetched_ts) if fetched_ts else 0.0
        result = {}
        for sym, e in cache.items():
            result[sym] = {
                "sealed": e.get(sealed_key),
                "vol": e.get(vol_key),
                "ready": True,
                "age": age,
            }
        return result

    def _read_from_parquet(self, target_date: date, is_down: bool) -> dict:
        if not self._repo:
            return {}
        out = self._repo.store.data_dir / "depth5" / f"date={target_date.isoformat()}" / "part.parquet"
        if not out.exists():
            return {}
        try:
            df = pl.read_parquet(out)
        except Exception as e:  # noqa: BLE001
            logger.warning("depth5 parquet 读取失败: %s", e)
            return {}
        sealed_key = "sealed_down" if is_down else "sealed_up"
        # 封单量: 涨停=买一量, 跌停=卖一量
        vol_key = "ask1_vol" if is_down else "bid1_vol"
        result = {}
        for row in df.to_dicts():
            sym = row.get("symbol")
            if not sym:
                continue
            result[sym] = {
                "sealed": row.get(sealed_key),
                "vol": row.get(vol_key),
                "ready": True,
                "age": None,  # 盘后定版, 无 age
            }
        return result

    def is_sealed_ready(self, target_date: date) -> bool:
        """sealed 数据是否就绪(供前端降级判定)。"""
        # 内存缓存对应的数据日 == 查询日 → 看内存就绪状态
        if self._sealed_date and target_date == self._sealed_date:
            return self._sealed_ready
        # 其他日期: 有 parquet 就 ready
        return self._persisted_for_date(target_date)

    def get_sealed_age(self, target_date: date) -> float | None:
        """返回 sealed 数据 age(秒), 盘后定版为 None。"""
        if self._sealed_date and target_date == self._sealed_date and self._sealed_ready and self._sealed_fetched_ts:
            return time.perf_counter() - self._sealed_fetched_ts
        return None

    # ================================================================
    # 盘中轮询线程
    # ================================================================

    def _poll_loop(self) -> None:
        """盘中轮询: 按 capset 自适应间隔拉 depth, 更新内存缓存。"""
        while self._running:
            try:
                if self._is_continuous_trading():
                    self._poll_once()
                else:
                    logger.debug("depth sealed: 非连续竞价时段, 跳过(避免集合竞价盘口覆盖 11:30 定格值)")
            except Exception as e:  # noqa: BLE001
                logger.warning("depth sealed 轮询异常: %s", e)

            # 等待下一轮(用 _running 检查保证能及时退出)
            interval = self._current_sleep_interval()
            waited = 0.0
            while self._running and waited < interval:
                time.sleep(0.5)
                waited += 0.5

    def _poll_once(self) -> None:
        """单次轮询: 算间隔(三层防护) → 拉取 → 检测系统接管通知。"""
        # 数当前涨跌停股
        n = self._count_limit_stocks()
        if n == 0:
            return

        interval, taken_over, user_interval = self._compute_interval(n)

        # 系统接管通知(状态切换时才推, 防刷屏)
        if taken_over and (self._last_taken_over is False or self._last_user_interval != user_interval):
            self._notify_takeover(n, user_interval, interval)
        self._last_taken_over = taken_over
        self._last_user_interval = user_interval

        self._fetch_and_seal(persist=False)

    def _current_sleep_interval(self) -> float:
        """计算当前 sleep 间隔(供 _poll_loop 等待用)。"""
        n = self._count_limit_stocks()
        if n == 0:
            return 30.0  # 无涨跌停, 慢轮询
        interval, _, _ = self._compute_interval(n)
        return interval

    # ================================================================
    # 三层防护节流
    # ================================================================

    def _compute_interval(self, n_symbols: int) -> tuple[float, bool, float]:
        """三层防护计算实际轮询间隔。

        返回 (actual_interval, taken_over, user_interval)
        - actual_interval: 实际使用的间隔(秒)
        - taken_over: 是否被系统接管(用户设置会超限)
        - user_interval: 用户设置(经套餐 clamp 后)的间隔
        """
        from app.services import preferences
        from app.tickflow.policy import tier_label

        capset = self._get_capset()
        lim = capset.limits(__import__("app.tickflow.capabilities", fromlist=["Cap"]).Cap.DEPTH5_BATCH)
        batch_size = (lim.batch if lim and lim.batch else 100)
        rpm = (lim.rpm if lim and lim.rpm else 30)

        # ① 套餐范围 clamp
        tier = tier_label().split()[0].split("+")[0].strip().lower()
        lo, hi = TIER_INTERVAL_RANGE.get(tier, DEFAULT_RANGE)
        raw_user = preferences.get_depth_polling_interval()
        user_interval = max(lo, min(hi, raw_user))

        # ② 限速安全 clamp
        batches = max(1, math.ceil(n_symbols / batch_size))
        usable_rpm = rpm * RPM_MARGIN
        calls_per_min = usable_rpm / batches if batches > 0 else usable_rpm
        safe_interval = 60.0 / calls_per_min if calls_per_min > 0 else INTERVAL_HARD_MAX

        # 实际: 取用户设置和安全的较大值
        actual = max(user_interval, safe_interval)
        # 硬上下限
        actual = max(INTERVAL_HARD_MIN, min(actual, INTERVAL_HARD_MAX))
        taken_over = safe_interval > user_interval

        return actual, taken_over, user_interval

    def _count_limit_stocks(self) -> int:
        """数当前涨跌停股总数(供节流计算)。"""
        if not self._repo:
            return 0
        enriched, _ = self._repo.get_enriched_latest()
        if enriched.is_empty():
            return 0
        n = 0
        if "signal_limit_up" in enriched.columns:
            n += enriched.filter(pl.col("signal_limit_up").fill_null(False)).height
        if "signal_limit_down" in enriched.columns:
            n += enriched.filter(pl.col("signal_limit_down").fill_null(False)).height
        return n

    # ================================================================
    # 通知
    # ================================================================

    def _notify_takeover(self, n_stocks: int, user_interval: float, actual_interval: float) -> None:
        """系统接管通知: 通过 quote_service 广播到所有 SSE 订阅者。"""
        if not self._app_state:
            return
        qs = getattr(self._app_state, "quote_service", None)
        if not qs:
            return
        msg = (f"五档轮询: 当前涨跌停 {n_stocks} 只, 您设置的 {user_interval:.0f} 秒间隔会超限, "
               f"系统已自动调整为 {actual_interval:.0f} 秒")
        alert = {
            "source": "depth",
            "type": "takeover",
            "message": msg,
        }
        try:
            qs.push_alerts([alert])
        except Exception as e:  # noqa: BLE001
            logger.debug("depth 接管通知推送失败: %s", e)

    def _notify_depth_updated(self, count: int) -> None:
        """修正完成通知: set quote_service._depth_update_event, SSE 推 depth_updated 刷新连板梯队。"""
        if not self._app_state:
            return
        qs = getattr(self._app_state, "quote_service", None)
        if not qs:
            return
        try:
            qs.notify_depth_updated()
        except Exception as e:  # noqa: BLE001
            logger.debug("depth 更新通知推送失败: %s", e)

    # ================================================================
    # 工具
    # ================================================================

    def _has_capability(self) -> bool:
        capset = self._get_capset()
        from app.tickflow.capabilities import Cap
        return capset.has(Cap.DEPTH5_BATCH)

    def _get_capset(self):
        """获取当前 capset(优先 app.state, 回退 detect)。"""
        if self._app_state:
            cs = getattr(self._app_state, "capabilities", None)
            if cs:
                return cs
        from app.tickflow.policy import detect_capabilities
        return detect_capabilities()

    @staticmethod
    def _is_trading_hours() -> bool:
        # 显式北京时间: 容器/服务器本地时区可能是 UTC, 用 naive now() 会整体错开轮询窗口
        now = cn_now()
        t = now.time()
        morning = dt_time(9, 25) <= t <= dt_time(11, 35)
        afternoon = dt_time(12, 55) <= t <= dt_time(15, 5)
        return now.weekday() < 5 and (morning or afternoon)

    @staticmethod
    def _is_continuous_trading() -> bool:
        """A股连续竞价时段(北京时间): 9:30-11:30 / 13:00-15:00, 仅工作日。

        比 _is_trading_hours 严格: 排除午间休市前后(11:30-13:00)。
        depth sealed 轮询用此窗口而非宽窗口, 关键原因:
        12:55-13:00 午后集合竞价准备期, ask1/bid1 盘口语义与连续竞价不同,
        「涨停价上卖一==0」的真封判定在此期间失效, 会用竞价盘口覆盖 11:30
        已定格的正确 sealed 值, 导致涨停股误判为 sealed=False(假涨停)被错误扣减。
        与 quote_service._is_continuous_trading 窗口定义保持一致。
        """
        now = cn_now()
        t = now.time()
        morning = dt_time(9, 30) <= t <= dt_time(11, 30)
        afternoon = dt_time(13, 0) <= t <= dt_time(15, 0)
        return now.weekday() < 5 and (morning or afternoon)
