"""全局实时行情服务。

集中管理全市场行情拉取 + enriched 缓存，供盘中选股、自选股等所有模块复用。

架构:
  - 后台线程轮询 TickFlow get_by_universes(["CN_Equity_A", "CN_Index"])
  - 拉取行情 → 写 kline_daily (不复权) + 增量计算 enriched → 写盘 + 更新缓存
  - _enriched_cache 是唯一的盘中数据源 (OHLCV + 全套技术指标)
  - _live_agg_cache 是递推状态 (只加载一次, 盘中不变)

数据流 (每轮 ~15s):
  1. API 拉取 → raw_records (临时变量)
  2. raw_records → 写 kline_daily (不复权原始价格)
  3. raw_records → 更新 _enriched_cache 的 OHLCV
  4. 增量计算 enriched 指标 (~50ms)
  5. 写 kline_daily_enriched + 替换 _enriched_cache
  6. 通知 SSE

生命周期:
  - 服务启动时读取 preferences，若 enabled 则自动启动线程
  - 运行中可通过 API 切换开关
  - 关闭时停止线程
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, time as dt_time

import polars as pl

from app.market_time import cn_now, cn_today
from app.parquet import scan_daily_parquet

logger = logging.getLogger(__name__)

# Webhook(飞书等)投递专用线程池 —— 与行情轮询线程隔离。
# send_feishu 内置重试(最坏 ~3×5s 超时 + 退避), 若在 _poll_loop 上同步投递,
# webhook 慢/宕机会逐条累加, 拖垮整条实时行情+告警轮询。这里 fire-and-forget,
# 失败由 webhook_adapter 记 WARNING(可见), 但绝不阻塞热路径。
_WEBHOOK_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="feishu-webhook")


class QuoteSubscriber:
    """一个 SSE 连接对应一个订阅者: 独立事件 + 独立队列。

    此前四个通道共用服务级 Event + pending 列表, pop 是「取走」语义:
    多客户端 (多标签页/多设备) 时告警只会被先醒来的连接消费, 其余永远
    收不到; 共享 Event 的 clear/wait 也存在互相吞信号的竞态。
    改为每连接独立订阅者后, 事件对所有客户端广播。
    """

    def __init__(self, max_alerts: int = 1000, max_reviews: int = 200) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._max_alerts = max_alerts
        self._max_reviews = max_reviews
        self._quote_updated = False
        self._strategy_results_updated = False
        self._depth_updated = False
        self._alerts: list[dict] = []
        self._reviews: list[str] = []

    # ── 消费侧 (SSE generator 线程) ──────────────────────
    def wait(self, timeout: float = 5.0) -> bool:
        """阻塞等待任一通道有新信号。"""
        return self._event.wait(timeout=timeout)

    def pop(self) -> dict:
        """原子取走全部待推送内容并复位事件。"""
        with self._lock:
            out = {
                "quote_updated": self._quote_updated,
                "strategy_results_updated": self._strategy_results_updated,
                "depth_updated": self._depth_updated,
                "alerts": self._alerts,
                "reviews": self._reviews,
            }
            self._quote_updated = False
            self._strategy_results_updated = False
            self._depth_updated = False
            self._alerts = []
            self._reviews = []
            self._event.clear()
            return out

    # ── 生产侧 (行情轮询 / depth / 复盘线程) ─────────────
    def push_alerts(self, alerts: list[dict]) -> None:
        with self._lock:
            self._alerts.extend(alerts)
            if len(self._alerts) > self._max_alerts:  # 背压: 丢弃最旧
                self._alerts = self._alerts[-self._max_alerts:]
            self._event.set()

    def push_review(self, event_json: str) -> None:
        with self._lock:
            self._reviews.append(event_json)
            if len(self._reviews) > self._max_reviews:
                self._reviews = self._reviews[-self._max_reviews:]
            self._event.set()

    def clear_alerts(self) -> None:
        with self._lock:
            self._alerts = []
            if (
                not self._quote_updated
                and not self._strategy_results_updated
                and not self._depth_updated
                and not self._reviews
            ):
                self._event.clear()

    def notify_quote(self) -> None:
        with self._lock:
            self._quote_updated = True
            self._event.set()

    def notify_strategy_results(self) -> None:
        with self._lock:
            self._strategy_results_updated = True
            self._event.set()

    def notify_depth(self) -> None:
        with self._lock:
            self._depth_updated = True
            self._event.set()


def _persist_last_fetch(fetched_at_ms: float) -> None:
    """把"最后获取"时间戳持久化到 preferences, 使进程重启后仍可显示。

    放在锁外调用 (IO); 失败不影响主流程 (内存值已更新, 下次 fetch 再写)。
    """
    try:
        from app.services import preferences
        preferences.save({"last_fetch_ms": round(fetched_at_ms, 0)})
    except Exception as e:  # noqa: BLE001
        logger.debug("last_fetch_ms 持久化失败 (不影响行情): %s", e)


class QuoteService:
    """全局实时行情服务 — 单例。"""

    CORE_INDEX_SYMBOLS = ("000001.SH", "399001.SZ", "399006.SZ", "000680.SH")

    # 档位 → 最小轮询间隔 (秒)
    TIER_MIN_INTERVAL = {
        "expert": 1.0,
        "pro": 3.0,
        "starter": 6.0,
        "free": 6.0,
    }
    DEFAULT_INTERVAL = 6.0
    MAX_INTERVAL = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 串行化行情拉取: 手动 POST /refresh 与后台轮询线程可能并发调用
        # _fetch_quotes, 两者同时写同一批 parquet/缓存会互相覆盖
        self._fetch_lock = threading.Lock()
        self._running = False
        self._enabled = False      # 全局开关 (持久化到 preferences)
        # 暂停态: 盘后管道/数据修正运行期间临时暂停取数, 防止与管道写同一批 parquet 竞态。
        # 与 _enabled 不同 — pause 不改 preferences、不 stop 线程, 仅让轮询循环跳过取数;
        # 进程重启后 _paused 归零, 从 preferences 恢复真实开关态, 无"假关闭"副作用。
        self._paused = False
        self._interval = self.DEFAULT_INTERVAL
        self._thread: threading.Thread | None = None
        self._repo = None          # 延迟注入, 避免循环导入
        # SSE 订阅者集合: 每个 /stream 连接一个 QuoteSubscriber, 事件广播到所有订阅者
        self._subscribers: set[QuoteSubscriber] = set()
        self._strategy_monitor = None            # 延迟注入
        self._app_state = None                   # 延迟注入 (FastAPI app.state)

        # 拉取元信息 (给 SSE / status 用)
        self._fetch_time: float = 0.0       # perf_counter (用于计算 quote_age_ms)
        self._fetch_ms: float = 0.0         # 拉取耗时 (毫秒)
        # _fetched_at 持久化到 preferences: 进程重启后仍能显示"最后获取"时间,
        # 不因关闭开关/重启而归零 (数据页卡片常驻显示, 方便判断上次拉取时刻)。
        try:
            from app.services import preferences as _prefs
            self._fetched_at: float = float(_prefs.load().get("last_fetch_ms", 0.0))
        except Exception:  # noqa: BLE001
            self._fetched_at = 0.0      # 拉取完成的 Unix 时间戳 (毫秒)
        self._symbol_count: int = 0
        self._index_symbol_count: int = 0
        self._etf_symbol_count: int = 0
        self._index_quotes_cache: pl.DataFrame | None = None
        # 午休/收盘最终同步状态: 到边界后必须成功拉取一版行情, 再进入休盘态。
        self._final_sync_done: set[tuple[date, str]] = set()
        self._final_sync_failed: dict[tuple[date, str], str] = {}

    # ================================================================
    # 生命周期
    # ================================================================

    def start(self, interval: float = 0.0) -> None:
        """启动后台行情轮询线程。"""
        if self._running:
            return
        if interval <= 0:
            from app.services import preferences
            interval = preferences.get_realtime_quote_interval()
        self._interval = self._clamp_interval(interval)
        self._running = True
        self._enabled = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self._save_enabled(True)
        logger.info("行情服务已启动, 轮询间隔 %.1fs", self._interval)

    def stop(self) -> None:
        """停止后台行情轮询线程。"""
        self._running = False
        self._enabled = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        self._save_enabled(False)
        logger.info("行情服务已停止")

    def enable(self) -> bool:
        """开启自动行情 (不立即启动线程，等下一个交易时段)。

        none 档无实时行情权限,拒绝开启并返回 False;
        free 档开启自选股实时,starter+ 开启全市场实时。返回值表示是否真正开启。
        """
        if not self.is_realtime_allowed():
            logger.warning("实时行情开启被拒:当前档位(none)无实时行情权限")
            return False
        self._enabled = True
        self._save_enabled(True)
        if not self._running:
            from app.services import preferences
            self._interval = self._clamp_interval(preferences.get_realtime_quote_interval())
            self._running = True
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()
        logger.info("行情服务已启用, 轮询间隔 %.1fs", self._interval)
        return True

    def disable(self) -> None:
        """关闭自动行情。"""
        self.stop()
        logger.info("行情服务已关闭")

    # ================================================================
    # 临时暂停 (盘后管道/数据修正期间, 防止写盘竞态)
    # ================================================================

    def pause(self) -> None:
        """临时暂停行情轮询取数 (不关闭线程、不改 preferences)。

        用于盘后管道/数据修正运行期间, 防止实时行情覆写管道正在写的 parquet。
        与 stop() 的区别: 线程继续存活但跳过 _fetch_quotes; preferences 开关态不变,
        管道结束调用 resume() 即恢复。线程级检查, 即时生效, 无 join 等待。
        """
        self._paused = True
        logger.info("行情轮询已临时暂停 (管道/修正运行中)")

    def resume(self) -> None:
        """恢复暂停的行情轮询取数 (对应 pause)。"""
        self._paused = False
        logger.info("行情轮询已恢复")

    def is_paused(self) -> bool:
        """是否处于临时暂停态 (管道运行期间)。"""
        return self._paused

    @contextmanager
    def paused(self):
        """上下文管理器: 进入时暂停轮询取数, 退出时(含异常)自动恢复。

        供盘后管道/数据修正复用:
            with quote_service.paused():
                run_pipeline(...)
        无论正常结束还是异常/crash, finally 都会 resume (除非进程直接被 kill)。
        """
        self.pause()
        try:
            yield
        finally:
            self.resume()

    def boot_check(self) -> None:
        """启动时检查 preferences，若 enabled 则自动启动。

        none 档无实时行情权限:即使 preferences 标记为 enabled,
        也不启动,并同步 preferences 为关闭(避免 UI 误显示已开启)。
        """
        from app.services import preferences
        if not self.is_realtime_allowed():
            if preferences.get_realtime_quotes_enabled():
                self._save_enabled(False)
            logger.info("实时行情未启动:当前档位(none)无实时行情权限")
            return
        if preferences.get_realtime_quotes_enabled():
            self.start()

    def set_repo(self, repo) -> None:
        """注入 KlineRepository, 用于实时落盘。"""
        self._repo = repo

    def set_app_state(self, app_state) -> None:
        """注入 FastAPI app.state, 用于获取 strategy_monitor 等单例。"""
        self._app_state = app_state

    def set_interval(self, interval: float) -> float:
        """运行时更新轮询间隔（立即生效）。"""
        clamped = self._clamp_interval(interval)
        self._interval = clamped
        from app.services import preferences
        preferences.set_realtime_quote_interval(clamped)
        logger.info("轮询间隔已更新为 %.1fs", clamped)
        return clamped

    def get_min_interval(self) -> float:
        """返回当前档位允许的最小间隔。"""
        return self._tier_min_interval()

    # ================================================================
    # SSE 订阅管理 — 每个 /stream 连接一个订阅者, 事件广播
    # ================================================================

    def subscribe(self) -> QuoteSubscriber:
        """注册一个 SSE 订阅者 (连接建立时调用)。"""
        sub = QuoteSubscriber()
        with self._lock:
            self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: QuoteSubscriber) -> None:
        """注销订阅者 (连接断开时调用)。"""
        with self._lock:
            self._subscribers.discard(sub)

    def _snapshot_subscribers(self) -> list[QuoteSubscriber]:
        with self._lock:
            return list(self._subscribers)

    def _broadcast_quote_updated(self) -> None:
        # 实时行情刷新后清空总览聚合缓存, 使看板 (overview-market) 在 SSE 触发的
        # 重取中拿到最新指数/聚合值。与 _broadcast 同时进行, 与侧栏 /intraday/indices
        # (无缓存, 直读实时缓存) 行为对齐, 避免看板落后于侧栏。
        # 延迟导入规避 services <-> api 层循环依赖。
        from app.api.overview import invalidate_overview_cache

        invalidate_overview_cache()
        for sub in self._snapshot_subscribers():
            sub.notify_quote()

    def notify_strategy_results_updated(self) -> None:
        """策略监控完成实时结果更新后调用，仅刷新策略页结果缓存。"""
        for sub in self._snapshot_subscribers():
            sub.notify_strategy_results()

    def notify_depth_updated(self) -> None:
        """五档盘口修正完成后调用: 通知 SSE 推送 depth_updated, 触发连板梯队刷新。

        与行情/告警通道独立 — 只刷新连板梯队, 不连带刷新 watchlist 等。
        """
        for sub in self._snapshot_subscribers():
            sub.notify_depth()

    def _broadcast_alerts(self, alerts: list[dict]) -> None:
        for sub in self._snapshot_subscribers():
            sub.push_alerts(alerts)

    def push_alerts(self, alerts: list[dict]) -> None:
        self._broadcast_alerts(alerts)

    def clear_pending_alerts(self) -> None:
        for sub in self._snapshot_subscribers():
            sub.clear_alerts()

    def push_review_event(self, event_json: str) -> None:
        """广播一条复盘进度事件(JSON 字符串), 唤醒所有 SSE generator。

        事件格式与 recap_market_stream 的产出一致(meta/delta/error/done),
        前端 reviewStore 直接消费。背压在订阅者队列内做 (丢弃最旧)。
        """
        for sub in self._snapshot_subscribers():
            sub.push_review(event_json)

    # ================================================================
    # 档位感知间隔限制
    # ================================================================

    @staticmethod
    def _current_tier() -> str:
        """获取当前档位名（小写）。"""
        from app.tickflow.policy import tier_label
        return tier_label().split()[0].split("+")[0].strip().lower()

    @classmethod
    def realtime_mode(cls) -> str:
        """当前实时行情模式: none / watchlist / full_market。"""
        from app.services import preferences
        if preferences.get_realtime_data_provider() != "tickflow":
            return "full_market"
        tier = cls._current_tier()
        if tier == "none":
            return "none"
        if tier == "free":
            return "watchlist"
        return "full_market"

    @classmethod
    def is_realtime_allowed(cls) -> bool:
        """当前档位是否允许使用实时行情。"""
        return cls.realtime_mode() != "none"

    @classmethod
    def _tier_min_interval(cls) -> float:
        tier = cls._current_tier()
        return cls.TIER_MIN_INTERVAL.get(tier, cls.DEFAULT_INTERVAL)

    def _clamp_interval(self, interval: float) -> float:
        return max(self._tier_min_interval(), min(self.MAX_INTERVAL, interval))

    # ================================================================
    # 行情数据访问
    # ================================================================

    def get_enriched_today(self) -> tuple[pl.DataFrame, date | None]:
        """返回今天 enriched 数据 + 日期 (线程安全)。

        所有页面统一通过此方法获取实时行情 + 技术指标。
        """
        if not self._repo:
            return pl.DataFrame(), None
        return self._repo.get_enriched_latest()

    def get_quotes_compat(self) -> pl.DataFrame:
        """兼容接口: 返回行情 DataFrame (用于盘中选股等需要 last_price/prev_close 的场景)。

        从 _enriched_cache 取 today 的数据, 只选行情基础列, 补上 last_price 别名。
        不返回指标列, 避免 JOIN live_agg 时列名冲突。
        """
        df, _ = self.get_enriched_today()
        if df.is_empty():
            return df

        # 只取盘中选股需要的行情基础列
        keep = [c for c in [
            "symbol", "close", "open", "high", "low", "volume", "amount",
            "prev_close", "change_pct", "change_amount", "amplitude", "turnover_rate",
        ] if c in df.columns]
        df = df.select(keep)

        # enriched 的 close 等价于 last_price
        if "close" in df.columns and "last_price" not in df.columns:
            df = df.with_columns(pl.col("close").alias("last_price"))
        return df

    def get_index_quotes(self, symbols: list[str] | None = None) -> pl.DataFrame:
        """返回实时指数行情缓存。不会触发 TickFlow 请求。"""
        with self._lock:
            df = self._index_quotes_cache.clone() if self._index_quotes_cache is not None else pl.DataFrame()
        if df.is_empty():
            return df
        if symbols:
            return df.filter(pl.col("symbol").is_in(symbols))
        return df

    def status(self) -> dict:
        """返回行情服务状态。"""
        from app.services import preferences
        age = (time.perf_counter() - self._fetch_time) * 1000 if self._fetch_time else -1
        mode = self.realtime_mode()
        phase = self._market_phase()
        final_key = self._final_sync_key(phase)
        final_done = bool(final_key and final_key in self._final_sync_done)
        final_failed = self._final_sync_failed.get(final_key) if final_key else None
        return {
            "enabled": self._enabled,
            "running": self._running,
            "paused": self._paused,
            "mode": mode,
            "realtime_allowed": mode != "none",
            "watchlist_symbol_count": len(preferences.get_realtime_watchlist_symbols()),
            "interval_s": self._interval,
            "symbol_count": self._symbol_count,
            "index_symbol_count": self._index_symbol_count,
            "etf_symbol_count": self._etf_symbol_count,
            "quote_age_ms": round(age, 0) if age >= 0 else None,
            # 交易时段 = 连续竞价; polling_window 另行返回,避免午休/收盘缓冲误显示为交易中。
            "is_trading_hours": self._is_continuous_trading(),
            "is_polling_window": self._should_poll_for_phase(phase),
            "market_phase": phase,
            "final_sync_done": final_done,
            "final_sync_failed": final_failed,
            "last_fetch_ms": round(self._fetched_at, 0) if self._fetched_at else None,
        }

    def refresh(self) -> dict:
        """手动触发一次行情拉取。"""
        self._fetch_quotes()
        return self.status()

    # ================================================================
    # 后台轮询
    # ================================================================

    def _poll_loop(self) -> None:
        while self._running and self._enabled:
            try:
                # 管道/数据修正运行期间临时暂停取数, 防止与管道写同一批 parquet 竞态。
                # 线程继续存活 + 分片 sleep, resume() 后即时恢复, 无需重启线程。
                if not self._paused:
                    phase = self._market_phase()
                    if self._should_fetch_for_phase(phase):
                        is_final = phase in {"morning_final", "close_final"}
                        ok = self._fetch_quotes(final=is_final)
                        if is_final:
                            key = self._final_sync_key(phase)
                            if key and ok:
                                self._final_sync_done.add(key)
                                self._final_sync_failed.pop(key, None)
                                logger.info("%s 最终行情同步完成, 进入休盘态", "午休" if phase == "morning_final" else "收盘")
                            elif key:
                                self._final_sync_failed[key] = "fetch_failed"
                                logger.warning("%s 最终行情同步失败, 将继续重试", "午休" if phase == "morning_final" else "收盘")
                    else:
                        logger.debug("非轮询阶段(%s), 跳过行情轮询", phase)
            except Exception as e:  # noqa: BLE001
                logger.warning("行情轮询异常: %s", e)

            waited = 0.0
            while self._running and self._enabled and waited < self._interval:
                time.sleep(0.5)
                waited += 0.5

    def _fetch_quotes(self, *, final: bool = False) -> bool:
        """按当前档位拉取行情。加锁串行化 (后台轮询 vs 手动 refresh)。返回本轮是否成功更新。"""
        with self._fetch_lock:
            before = self._fetched_at
            if final:
                logger.info("最终行情同步开始")
            if self.realtime_mode() == "watchlist":
                self._fetch_watchlist_quotes()
            else:
                self._fetch_full_market_quotes()
            return self._fetched_at > before

    def _fetch_full_market_quotes(self) -> None:
        """拉取全市场行情 → 写 daily + 计算 enriched + 更新缓存。"""
        from app.services import preferences

        provider_name = preferences.get_realtime_data_provider()
        if provider_name != "tickflow":
            from app.data_providers import custom as custom_sources
            if custom_sources.provider_has_dataset(provider_name, "realtime"):
                try:
                    t0 = time.perf_counter()
                    now_ts = time.perf_counter()
                    records = custom_sources.get_provider(provider_name).get_realtime()
                except Exception as e:  # noqa: BLE001
                    logger.warning("自定义实时行情拉取失败: %s", e)
                    return
                self._process_full_market_records(records, t0=t0, now_ts=now_ts)
                return
            # 自定义源未配置 realtime → 回退 TickFlow

        from app.tickflow.client import get_paid_realtime_client

        tf = get_paid_realtime_client()
        if tf is None:
            logger.warning("实时行情拉取失败:未配置付费服务器 API Key")
            return
        t0 = time.perf_counter()
        now_ts = time.perf_counter()

        try:
            from app.services import preferences
            all_index_symbols = set(self._repo.get_index_symbol_set()) if self._repo else set()
            core_index_symbols = set(preferences.get_realtime_index_symbols() or self.CORE_INDEX_SYMBOLS)
            all_index_symbols.update(core_index_symbols)
            all_etf_symbols = set()
            if self._repo:
                etf_inst = self._repo.get_etf_instruments()
                if not etf_inst.is_empty() and "symbol" in etf_inst.columns:
                    all_etf_symbols = set(etf_inst["symbol"].cast(pl.Utf8).to_list())

            universes: list[str] = []
            if preferences.get_realtime_pull_stock():
                universes.append("CN_Equity_A")
            if preferences.get_realtime_pull_etf() and all_etf_symbols:
                universes.append("CN_ETF")
            if preferences.get_realtime_pull_index() and preferences.get_realtime_index_mode() == "all":
                universes.append("CN_Index")

            resp = []
            if universes:
                _u0 = time.perf_counter()
                logger.info("拉取全市场行情 (universes=%s, SDK超时=30s×重试3)", universes)
                resp.extend(tf.quotes.get_by_universes(universes=universes) or [])
                logger.info("全市场行情拉取完成: %d 条 (%.2fs)", len(resp), time.perf_counter() - _u0)
            if preferences.get_realtime_pull_index() and preferences.get_realtime_index_mode() == "core":
                _i0 = time.perf_counter()
                _core_syms = sorted(core_index_symbols)
                resp.extend(tf.quotes.get(symbols=_core_syms) or [])
                logger.info("核心指数行情拉取完成: %d 只 (%.2fs)", len(_core_syms), time.perf_counter() - _i0)
        except Exception as e:  # noqa: BLE001
            logger.warning("行情拉取失败 (%.2fs): %s", time.perf_counter() - t0, e)
            return

        if not resp:
            logger.warning("行情数据为空")
            return

        # ---- 解析 API 响应 (临时变量, 用完丢弃) ----
        records = []
        for q in resp:
            ext = q.get("ext") or {}
            last_price = q.get("last_price")
            prev_close = q.get("prev_close")
            change_amount = ext.get("change_amount")
            change_pct = ext.get("change_pct")
            if change_amount is None and last_price is not None and prev_close is not None:
                change_amount = float(last_price) - float(prev_close)
            if change_pct is None and change_amount is not None and prev_close not in (None, 0):
                # 与 API ext.change_pct 同为小数制 (0.0366 = 3.66%),
                # enriched 全项目约定小数 (见 pipeline.py), 此处不可乘 100
                change_pct = float(change_amount) / float(prev_close)
            records.append({
                "symbol": q.get("symbol"),
                "name": q.get("name") or ext.get("name"),
                "last_price": last_price,
                "prev_close": prev_close,
                "open": q.get("open"),
                "high": q.get("high"),
                "low": q.get("low"),
                "volume": q.get("volume"),
                "amount": q.get("amount"),
                "change_pct": change_pct,
                "change_amount": change_amount,
                "amplitude": ext.get("amplitude"),
                "turnover_rate": ext.get("turnover_rate"),
                "timestamp": q.get("timestamp"),
                "session": q.get("session"),
            })

        self._process_full_market_records(records, t0=t0, now_ts=now_ts)

    def _process_full_market_records(self, records: list[dict], *, t0: float, now_ts: float) -> None:
        """把全市场 records 写盘并增量计算 enriched。"""
        from app.services import preferences
        all_index_symbols = set(self._repo.get_index_symbol_set()) if self._repo else set()
        core_index_symbols = set(preferences.get_realtime_index_symbols() or self.CORE_INDEX_SYMBOLS)
        all_index_symbols.update(core_index_symbols)
        all_etf_symbols = set()
        if self._repo:
            etf_inst = self._repo.get_etf_instruments()
            if not etf_inst.is_empty() and "symbol" in etf_inst.columns:
                all_etf_symbols = set(etf_inst["symbol"].cast(pl.Utf8).to_list())

        if not records:
            logger.warning("行情数据为空")
            return

        index_records = [r for r in records if r.get("symbol") in all_index_symbols]
        etf_records = [r for r in records if r.get("symbol") in all_etf_symbols]
        stock_records = [
            r for r in records
            if r.get("symbol") not in all_index_symbols and r.get("symbol") not in all_etf_symbols
        ]

        fetch_ms = (time.perf_counter() - t0) * 1000
        fetched_at = time.time() * 1000

        # ---- 更新元信息 ----
        with self._lock:
            self._fetch_time = now_ts
            self._fetch_ms = fetch_ms
            self._fetched_at = fetched_at
            self._symbol_count = len(stock_records)
            self._index_symbol_count = len(index_records)
            self._etf_symbol_count = len(etf_records)
            self._index_quotes_cache = self._build_index_quotes(index_records)

        _persist_last_fetch(fetched_at)
        logger.info("行情刷新: %d 只股票, %d 只ETF, %d 只指数, 耗时 %.0fms", len(stock_records), len(etf_records), len(index_records), fetch_ms)

        # ---- 写 kline_daily (不复权原始价格, 只有 OHLCV) ----
        daily_df = self._build_daily(stock_records)
        if not daily_df.is_empty() and self._repo:
            try:
                self._repo.flush_live_daily(daily_df)
            except Exception as e:  # noqa: BLE001
                logger.warning("日K写盘失败: %s", e)

        etf_daily_df = self._build_daily(etf_records)
        if not etf_daily_df.is_empty() and self._repo:
            try:
                self._repo.flush_live_daily_asset("etf", etf_daily_df)
            except Exception as e:  # noqa: BLE001
                logger.warning("ETF 日K写盘失败: %s", e)

        # ---- 构建 API 直接值的补充表 (不写 daily, 只用于 enriched 计算) ----
        quote_extra = self._build_quote_extra(stock_records)
        etf_quote_extra = self._build_quote_extra(etf_records)

        # ---- 增量计算 enriched + 写盘 + 更新缓存 ----
        if not daily_df.is_empty() and self._repo:
            self._flush_live_enriched(daily_df, quote_extra, asset_type="stock")
        if not etf_daily_df.is_empty() and self._repo:
            self._flush_live_enriched(etf_daily_df, etf_quote_extra, asset_type="etf")

        # ---- 通知 SSE ----
        self._broadcast_quote_updated()

        # ---- 策略监控 + 告警评估 ----
        self._evaluate_monitors(daily_df, quote_extra)

    def _fetch_watchlist_quotes(self) -> None:
        """Free 档自选股实时: 只拉取最多 5 个 symbols。"""
        from app.services import preferences
        from app.tickflow.client import get_paid_realtime_client

        symbols = preferences.get_realtime_watchlist_symbols()
        if not symbols:
            logger.info("自选实时未配置标的, 跳过行情拉取")
            return

        tf = get_paid_realtime_client()
        if tf is None:
            logger.warning("自选实时拉取失败:未配置付费服务器 API Key")
            return

        t0 = time.perf_counter()
        now_ts = time.perf_counter()
        try:
            resp = tf.quotes.get(symbols=symbols) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("自选实时拉取失败: %s", e)
            return

        if not resp:
            logger.warning("自选实时行情数据为空")
            return

        records = []
        for q in resp:
            ext = q.get("ext") or {}
            last_price = q.get("last_price")
            prev_close = q.get("prev_close")
            change_amount = ext.get("change_amount")
            change_pct = ext.get("change_pct")
            if change_amount is None and last_price is not None and prev_close is not None:
                change_amount = float(last_price) - float(prev_close)
            if change_pct is None and change_amount is not None and prev_close not in (None, 0):
                # 小数制, 与 ext.change_pct / enriched 口径一致 (不乘 100)
                change_pct = float(change_amount) / float(prev_close)
            records.append({
                "symbol": q.get("symbol"),
                "name": q.get("name") or ext.get("name"),
                "last_price": last_price,
                "prev_close": prev_close,
                "open": q.get("open"),
                "high": q.get("high"),
                "low": q.get("low"),
                "volume": q.get("volume"),
                "amount": q.get("amount"),
                "change_pct": change_pct,
                "change_amount": change_amount,
                "amplitude": ext.get("amplitude"),
                "turnover_rate": ext.get("turnover_rate"),
                "timestamp": q.get("timestamp"),
                "session": q.get("session"),
            })

        fetch_ms = (time.perf_counter() - t0) * 1000
        fetched_at = time.time() * 1000
        with self._lock:
            self._fetch_time = now_ts
            self._fetch_ms = fetch_ms
            self._fetched_at = fetched_at
            self._symbol_count = len(records)
            self._index_symbol_count = 0
            self._etf_symbol_count = 0
            self._index_quotes_cache = None

        _persist_last_fetch(fetched_at)
        logger.info("自选实时刷新: %d 只股票, 耗时 %.0fms", len(records), fetch_ms)

        daily_df = self._build_daily(records)
        quote_extra = self._build_quote_extra(records)
        if not daily_df.is_empty() and self._repo:
            try:
                self._repo.merge_live_daily_asset("stock", daily_df)
            except Exception as e:  # noqa: BLE001
                logger.warning("自选实时日K写盘失败: %s", e)
            self._flush_live_enriched(daily_df, quote_extra, asset_type="stock", merge=True)

        self._broadcast_quote_updated()
        self._evaluate_monitors(daily_df, quote_extra)

    # ================================================================
    # 工具
    # ================================================================

    @staticmethod
    def _build_daily(records: list[dict]) -> pl.DataFrame:
        """将 API records 转为日K格式 DataFrame (OHLCV + quote_ts, 写 kline_daily 用)。"""
        if not records:
            return pl.DataFrame()
        df = pl.DataFrame(records)
        cols_map = {
            "symbol": "symbol",
            "last_price": "close",
            "open": "open",
            "high": "high",
            "low": "low",
            "volume": "volume",
            "amount": "amount",
            "timestamp": "quote_ts",
        }
        select_exprs = []
        for src, dst in cols_map.items():
            if src in df.columns:
                select_exprs.append(pl.col(src).cast(pl.Int64, strict=False).alias(dst)
                                     if dst == "quote_ts" else pl.col(src).alias(dst))
        if not select_exprs:
            return pl.DataFrame()
        result = df.select(select_exprs).with_columns(
            pl.lit(cn_today()).cast(pl.Date).alias("date"),
        )
        # 修复: API 在非交易时段可能返回 open/high/low=0 或 null,
        # 导致蜡烛从 0 开始。用 close 填充这些异常值。
        for col in ("open", "high", "low"):
            if col in result.columns:
                result = result.with_columns(
                    pl.when((pl.col(col) == 0) | pl.col(col).is_null())
                    .then(pl.col("close"))
                    .otherwise(pl.col(col))
                    .alias(col)
                )
        return result

    @staticmethod
    def _build_quote_extra(records: list[dict]) -> pl.DataFrame:
        """构建 API 直接提供的补充字段 (不写 daily, 只传给 enriched 计算)。

        包含: prev_close, change_pct, change_amount, amplitude, turnover_rate。
        """
        if not records:
            return pl.DataFrame()
        df = pl.DataFrame(records)
        keep = [c for c in [
            "symbol", "prev_close", "change_pct", "change_amount",
            "amplitude", "turnover_rate",
        ] if c in df.columns]
        if not keep or "symbol" not in keep:
            return pl.DataFrame()
        out = df.select(keep)
        # 实时 API 的 turnover_rate 入口契约为小数制(0.05 = 5%).
        # enriched 内部统一存百分数值(5 = 5%), 后续页面/筛选直接展示和比较。
        if "turnover_rate" in out.columns:
            out = out.with_columns((pl.col("turnover_rate").cast(pl.Float64, strict=False) * 100).alias("turnover_rate"))
        return out

    @staticmethod
    def _build_index_quotes(records: list[dict]) -> pl.DataFrame:
        """构建指数实时行情缓存，不落股票 parquet。

        注意: API 返回的 change_pct/amplitude 是小数 (0.0366 = 3.66%),
        统一转成百分比输出, 与 _fallback_index_quotes_from_daily 口径一致
        (前端指数侧不×100, 直接 toFixed(2)% 展示)。
        """
        if not records:
            return pl.DataFrame()
        df = pl.DataFrame(records)
        keep = [c for c in [
            "symbol", "name", "last_price", "prev_close", "open", "high", "low",
            "volume", "amount", "change_pct", "change_amount", "amplitude", "timestamp", "session",
        ] if c in df.columns]
        if not keep or "symbol" not in keep:
            return pl.DataFrame()
        df = df.select(keep)
        # change_pct / amplitude: 小数 → 百分比 (统一指数展示口径)
        for col in ("change_pct", "amplitude"):
            if col in df.columns:
                df = df.with_columns((pl.col(col).cast(pl.Float64) * 100).alias(col))
        if "last_price" in df.columns and "close" not in df.columns:
            df = df.with_columns(pl.col("last_price").alias("close"))
        return df

    @staticmethod
    def _market_phase() -> str:
        """A股行情轮询阶段(北京时间)。

        final 阶段用于午休/收盘定版: 需要至少成功拉取一版边界后的行情, 才算进入休盘。
        """
        now = cn_now()
        if now.weekday() >= 5:
            return "closed"
        t = now.time()
        if dt_time(9, 15) <= t < dt_time(9, 30):
            return "preopen"
        if dt_time(9, 30) <= t < dt_time(11, 30):
            return "morning"
        if dt_time(11, 30) <= t < dt_time(12, 55):
            return "morning_final"
        if dt_time(12, 55) <= t < dt_time(13, 0):
            return "pre_afternoon"
        if dt_time(13, 0) <= t < dt_time(15, 0):
            return "afternoon"
        if t >= dt_time(15, 0):
            return "close_final"
        return "closed"

    @staticmethod
    def _final_sync_key(phase: str) -> tuple[date, str] | None:
        if phase == "morning_final":
            return (cn_today(), "morning")
        if phase == "close_final":
            return (cn_today(), "close")
        return None

    def _should_poll_for_phase(self, phase: str) -> bool:
        """是否处于会主动拉行情的阶段。final 阶段成功后即停止。"""
        if phase in {"preopen", "morning", "pre_afternoon", "afternoon"}:
            return True
        key = self._final_sync_key(phase)
        return bool(key and key not in self._final_sync_done)

    def _should_fetch_for_phase(self, phase: str) -> bool:
        return self._should_poll_for_phase(phase)

    def _is_trading_hours(self) -> bool:
        """行情轮询窗口(兼容旧调用): 包含盘前预热和未完成的午休/收盘定版。"""
        return self._should_poll_for_phase(self._market_phase())

    @staticmethod
    def _is_continuous_trading() -> bool:
        """A股连续竞价时段(北京时间): 9:30-11:30 / 13:00-15:00, 仅工作日。

        比 _is_trading_hours 严格: 排除 9:15-9:30 集合竞价(指示价, 非成交价)、
        午间与 15:00 后收盘缓冲。监控评估只在此窗口进行, 不对竞价/收盘后的陈旧价告警。
        (节假日由 _evaluate_monitors 里的「快照日期=当日」新鲜度判据兜底, 无需交易日历。)
        """
        now = cn_now()
        t = now.time()
        morning = dt_time(9, 30) <= t <= dt_time(11, 30)
        afternoon = dt_time(13, 0) <= t <= dt_time(15, 0)
        return now.weekday() < 5 and (morning or afternoon)

    @staticmethod
    def _save_enabled(enabled: bool) -> None:
        from app.services import preferences
        preferences.save({"realtime_quotes_enabled": enabled})

    # ================================================================
    # 策略监控
    # ================================================================

    def _evaluate_monitors(self, daily_df: pl.DataFrame, quote_extra: pl.DataFrame | None) -> None:
        """行情更新后评估统一监控规则引擎,并刷新策略结果缓存。"""
        try:
            # 仅在「交易日 + 连续竞价时段」评估监控 —— 避开集合竞价指示价、盘前/收盘后
            # 缓冲。轮询窗口(_is_trading_hours)更宽是为盘前预热/收盘捕捉, 但告警不应
            # 基于这些非连续竞价价格。
            if not self._is_continuous_trading():
                return
            # 获取 enriched 数据 (刚算好的)
            enriched_today, enriched_date = self.get_enriched_today()
            if enriched_today.is_empty():
                return
            # 快照日期必须是北京当日: 节假日或数据未刷新时 enriched_date 会落后于当日,
            # 说明市场未在交易 → 跳过。无需维护 A股交易日历即可挡住节假日与陈旧价告警。
            if enriched_date != cn_today():
                logger.debug("监控评估跳过: enriched 快照日期 %s 非当日 %s (节假日/数据未刷新)",
                             enriched_date, cn_today())
                return

            all_alerts: list[dict] = []
            rule_events: list[dict] = []
            engine = None

            # 通用监控规则评估 (统一引擎: signal/price/market/strategy)
            if self._app_state:
                engine = getattr(self._app_state, "monitor_engine", None)
                if engine and engine.rule_count > 0:
                    # 预构建 symbol → name 映射 (enriched 已 drop name 列, 引擎触发时回填用)。
                    # 含股票 + ETF 维表, 保证 ETF 监控告警也能回填名称。
                    try:
                        name_map: dict[str, str] = {}
                        inst_df = self._app_state.repo.get_instruments()
                        if not inst_df.is_empty() and "symbol" in inst_df.columns and "name" in inst_df.columns:
                            for row in inst_df.select(["symbol", "name"]).iter_rows(named=True):
                                if row.get("name"):
                                    name_map[row["symbol"]] = row["name"]
                        # 仅当存在 ETF 规则时补 ETF 维表 (股票名优先, setdefault 不覆盖股票)
                        if engine.has_asset_rules("etf"):
                            etf_inst = self._app_state.repo.get_etf_instruments()
                            if not etf_inst.is_empty() and "symbol" in etf_inst.columns and "name" in etf_inst.columns:
                                for row in etf_inst.select(["symbol", "name"]).iter_rows(named=True):
                                    if row.get("name"):
                                        name_map.setdefault(row["symbol"], row["name"])
                        if name_map:
                            engine.set_name_map(name_map)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("name_map 构建失败 (不影响监控): %s", e)
                    # 连板梯队封单监控: 有 ladder 规则时, 从 depth_service 注入封单量到 enriched
                    eval_df = enriched_today
                    if engine.has_rule_type("ladder"):
                        eval_df = self._inject_sealed_vol(enriched_today, enriched_date)
                    rule_events = engine.evaluate(eval_df, asset_type="stock")
                    if engine.consume_strategy_result_updates():
                        self.notify_strategy_results_updated()
                    # ETF 规则轮: 股票快照不含 ETF, 用 ETF enriched 快照单独评估。
                    # 独立 try —— ETF 轮任何异常都不得丢弃本轮已算出的股票告警。
                    # refresh=False —— 不在轮询线程上触发 ETF 冷缓存的同步重算 (缓存由 ETF 实时
                    # flush 焐热; 未焐热说明无 ETF 实时数据, 跳过本轮 ETF 评估)。
                    if engine.has_asset_rules("etf") and self._repo is not None:
                        try:
                            etf_enriched, _ = self._repo.get_enriched_latest_asset("etf", refresh=False)
                            if not etf_enriched.is_empty():
                                rule_events = rule_events + engine.evaluate(
                                    etf_enriched, asset_type="etf", reset_strategy_results=False,
                                )
                        except Exception as e:  # noqa: BLE001
                            logger.warning("ETF 监控评估失败 (不影响股票告警): %s", e)
                    if rule_events:
                        # 落盘到 alerts.jsonl
                        try:
                            from app.services import alert_store
                            alert_store.append_many(
                                self._app_state.repo.store.data_dir, rule_events,
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.warning("告警落盘失败: %s", e)
                        # 转为 SSE 推送格式 (兼容旧 alert schema)
                        for ev in rule_events:
                            all_alerts.append({
                                "source": ev["source"],
                                "type": ev["type"],
                                "rule_id": ev.get("rule_id"),
                                "strategy_id": ev.get("rule_id") if ev["source"] == "strategy" else None,
                                "symbol": ev["symbol"],
                                "name": ev["name"],
                                "message": ev["message"],
                                "price": ev["price"],
                                "change_pct": ev["change_pct"],
                                "signals": ev["signals"],
                                "severity": ev.get("severity", "info"),
                                "conditions": ev.get("conditions") or [],
                                "logic": ev.get("logic") or "and",
                            })

            # 策略页实时回显: 不写文件 (实时行情每轮更新 enriched, 写文件会被 read_cache
            # 的 mtime 校验判过期, 反复读不到)。监控引擎本轮已算出的结果存在内存
            # (latest_strategy_results), 由 /api/screener/cached 端点直接叠加读取。

            # 广播到所有 SSE 订阅者 (背压保护在订阅者队列内做)
            if all_alerts:
                # 按 symbol 富化行业/概念 ext 字段, 使 toast + 触发记录统一展示板块标签。
                self._enrich_alerts_ext(all_alerts)
                self._broadcast_alerts(all_alerts)
                logger.info("监控评估完成: %d 条通知", len(all_alerts))

                # 系统通知 (可选通道, 由 preferences 开关控制)。
                # cooldown 去重已在 MonitorRuleEngine 做过, 这里只负责转发。
                self._maybe_send_system_notifications(all_alerts)

            # Webhook 推送 (飞书等外部 IM, 由规则 webhook_channels 指定渠道)。
            # 紧随系统通知, 同样静默降级不阻断主流程。
            if rule_events:
                self._maybe_send_webhook(rule_events, engine)

        except Exception as e:  # noqa: BLE001
            logger.warning("监控评估失败: %s", e)

    def _enrich_alerts_ext(self, alerts: list[dict]) -> None:
        """就地给告警事件按 symbol 追加行业/概念 ext 字段。

        读 preferences.get_monitor_ext_fields() 取字段配置, 用 screener._load_ext_value_maps
        (带 parquet mtime 缓存) 富化。富化失败静默降级 (告警照常推送, 只是没标签)。
        每条事件新增 {configId}__{fieldName} 键 (与 watchlist/screener 输出约定一致)。
        """
        if not alerts or not self._app_state or self._repo is None:
            return
        try:
            from app.services import preferences
            fields = preferences.get_monitor_ext_fields()
            # 新结构 {field, maxTags, hiddenIndices}, 后端只需 .field
            parts = []
            for key in ("concept", "industry"):
                item = fields.get(key)
                if isinstance(item, dict) and item.get("field"):
                    parts.append(item["field"])
                elif isinstance(item, str) and item:
                    parts.append(item)  # 兼容旧格式
            if not parts:
                return
            ext_columns = ",".join(parts)
            from app.api.screener import _load_ext_value_maps
            value_maps = _load_ext_value_maps(self._repo, ext_columns)
            if not value_maps:
                return
            for ev in alerts:
                sym = ev.get("symbol")
                if not sym:
                    continue
                for out_col, vmap in value_maps.items():
                    ev[out_col] = vmap.get(str(sym))
        except Exception as e:  # noqa: BLE001
            logger.debug("告警 ext 富化失败 (不影响推送): %s", e)

    def _inject_sealed_vol(self, enriched_today: pl.DataFrame, enriched_date) -> pl.DataFrame:
        """从 depth_service 取封单量, 作为临时列 _sealed_vol 注入 enriched 副本。

        涨停封单(买一量) + 跌停封单(卖一量)合并, 供 ladder 规则评估。
        depth 未就绪时返回原 df (不注入, ladder 规则安全降级不触发)。
        """
        try:
            depth_svc = getattr(self._app_state, "depth_service", None)
            if not depth_svc:
                return enriched_today
            # enriched_date 可能是 date 或字符串, 统一为 date
            from datetime import date as date_cls
            target_date = enriched_date if isinstance(enriched_date, date_cls) else date_cls.fromisoformat(str(enriched_date))
            # 取涨停 + 跌停封单, 合并 {symbol: vol}
            up_map = depth_svc.get_sealed_map(target_date, is_down=False)
            down_map = depth_svc.get_sealed_map(target_date, is_down=True)
            sealed: dict[str, int] = {}
            for m in (up_map, down_map):
                for sym, info in m.items():
                    vol = (info or {}).get("vol")
                    if vol and vol > 0:
                        sealed[sym] = vol  # 后者覆盖前者 (同 symbol 不可能在涨跌停都封单)
            if not sealed:
                return enriched_today
            # 构造 (symbol, _sealed_vol) DataFrame, join 到 enriched 副本
            sealed_df = pl.DataFrame({
                "symbol": list(sealed.keys()),
                "_sealed_vol": list(sealed.values()),
            })
            # 若已有残留列先移除 (避免重复 join 报错)
            df = enriched_today.drop("_sealed_vol") if "_sealed_vol" in enriched_today.columns else enriched_today
            return df.join(sealed_df, on="symbol", how="left")
        except Exception as e:  # noqa: BLE001
            logger.debug("封单注入失败 (ladder 规则将不触发): %s", e)
            return enriched_today

    def _maybe_send_webhook(self, rule_events: list[dict], engine) -> None:
        """把告警通过 Webhook 推送到外部 IM (由规则 webhook_channels 指定渠道)。

        - 飞书 / 企业微信任一已配置即生效 (两个都没配才跳过)
        - 仅推送 webhook_channels 非空的规则触发的告警, 且只投递被勾选的渠道
        - 失败静默, 不阻断主流程
        - 去重: 复用 MonitorRuleEngine 的 cooldown, 此处不重复去重

        注意: 用 rule_events (含 rule_id) 而非重建后的 all_alerts,
        以便反查引擎规则判断是否启用推送。
        """
        try:
            from app.services import preferences
            from app.services import webhook_adapter

            feishu_url = preferences.get_feishu_webhook_url()
            feishu_secret = preferences.get_feishu_webhook_secret()
            wecom_url = preferences.get_wecom_webhook_url()
            # 两个通道都没配置才跳过
            if not feishu_url and not wecom_url:
                return

            # 反查规则, 过滤出启用推送的事件
            source_labels = {
                "strategy": "策略", "signal": "信号",
                "price": "价格", "market": "异动",
            }
            rules = engine.rules if engine is not None else {}
            enqueued = 0
            for ev in rule_events:
                rule = rules.get(ev.get("rule_id"))
                # webhook_channels 指定命中的渠道 (['feishu'] / ['wecom'] / ['feishu','wecom'] / []).
                # 空列表 = 该规则不推送。仅推送「渠道已选 + 对应地址已配置」的组合。
                channels = rule.get("webhook_channels") if rule else None
                if not channels:
                    continue
                source = ev.get("source", "")
                source_label = source_labels.get(source, source or "通知")
                symbol = ev.get("symbol") or ""
                name = ev.get("name") or ""
                message = ev.get("message") or ""
                title = f"TickFlow · {source_label}"
                body = f"{symbol} {name} {message}".strip() if symbol else (message or name)
                # 提交到独立线程池, 不阻塞行情轮询线程 (webhook 慢/重试不拖累实时行情+告警)。
                # 按渠道独立投递: 飞书 / 企业微信谁被勾选且已配置就推谁。
                # 应用内 alerts.jsonl 记录与 SSE 已在前面完成, 不依赖 webhook 成败,
                # 失败由 webhook_adapter 记 WARNING(可见)。
                if feishu_url and "feishu" in channels:
                    _WEBHOOK_EXECUTOR.submit(webhook_adapter.send_feishu, feishu_url, title, body, feishu_secret)
                    enqueued += 1
                if wecom_url and "wecom" in channels:
                    _WEBHOOK_EXECUTOR.submit(webhook_adapter.send_wecom, wecom_url, title, body)
                    enqueued += 1
            if enqueued:
                logger.info("Webhook 已提交 %d 条 (异步投递, 按渠道独立投递, 失败记 WARNING)", enqueued)
        except Exception as e:  # noqa: BLE001
            logger.warning("Webhook 提交异常 (不影响告警主流程): %s", e)

    def _maybe_send_system_notifications(self, all_alerts: list[dict]) -> None:
        """把告警转发到操作系统通知中心 (由 preferences 开关控制)。

        - 开关关闭: 直接返回
        - 开关开启: 逐条发系统通知; 失败静默, 不阻断主流程
        - 去重: 复用 MonitorRuleEngine 的 cooldown, 此处不重复去重
        - 批量策略事件 (symbol="") 聚合为一条通知, 避免刷屏
        """
        try:
            from app.services import preferences
            from app.services import notify_adapter

            if not preferences.get_system_notify_enabled():
                return

            for ev in all_alerts:
                # 通知标题: 用 source 分类 (策略/信号/价格/异动)
                source = ev.get("source", "")
                source_label = {
                    "strategy": "策略", "signal": "信号",
                    "price": "价格", "market": "异动",
                }.get(source, source or "通知")

                name = ev.get("name") or ""
                symbol = ev.get("symbol") or ""
                message = ev.get("message") or ""

                # 正文: 优先用现成 message, 拼上 symbol/name 让用户一眼定位
                if symbol:
                    body = f"{symbol} {name} {message}".strip()
                else:
                    body = message or name

                title = f"TickFlow · {source_label}"
                notify_adapter.notify(title, body)
        except Exception as e:  # noqa: BLE001
            logger.debug("系统通知发送异常 (不影响告警主流程): %s", e)

    @staticmethod
    def _get_strategy_monitor():
        """获取 StrategyMonitorService — 不再使用, 改用 _app_state 注入。"""
        return None

    # ================================================================
    # enriched 增量计算
    # ================================================================

    def _flush_live_enriched(self, daily_df: pl.DataFrame, quote_extra: pl.DataFrame = None, asset_type: str = "stock", merge: bool = False) -> None:
        """增量计算今天的 enriched: 用昨天的递推状态 + 今天 OHLCV → 只算今天 5500 行。

        quote_extra: API 直接提供的补充字段 (prev_close, change_pct 等),
                     不写 daily, 直接传给 compute_enriched_today 避免重复计算。
        """
        try:
            today = cn_today()
            t0 = time.perf_counter()

            # ---- 尝试增量路径 ----
            live_agg = self._repo.get_live_agg() if asset_type == "stock" else pl.DataFrame()
            prev_enriched, prev_date = (
                self._repo.get_enriched_latest()
                if asset_type == "stock"
                else self._repo.get_enriched_latest_asset(asset_type)
            )

            use_incremental = (
                asset_type == "stock"
                and not live_agg.is_empty()
                and not prev_enriched.is_empty()
                and prev_date is not None
            )

            if use_incremental:
                from app.indicators.pipeline import compute_enriched_today
                from app.market_time import trading_minutes_elapsed_from_ts, trading_minutes_elapsed
                instruments = self._repo.get_instruments()
                # 将 API 直接提供的补充字段 JOIN 到 daily_df
                today_ohlcv = daily_df
                if quote_extra is not None and not quote_extra.is_empty():
                    today_ohlcv = daily_df.join(quote_extra, on="symbol", how="left")
                # 量比时间折算: 优先用行情 quote_ts (真实成交时间), 缺失则兜底服务端时间
                elapsed_minutes: float | None = None
                if "quote_ts" in daily_df.columns and not daily_df.is_empty():
                    valid_ts = daily_df["quote_ts"].drop_nulls()
                    if not valid_ts.is_empty():
                        elapsed_minutes = trading_minutes_elapsed_from_ts(valid_ts.median())
                if elapsed_minutes is None:
                    elapsed_minutes = trading_minutes_elapsed()
                enriched_today = compute_enriched_today(
                    live_agg=live_agg,
                    prev_enriched=prev_enriched,
                    today_ohlcv=today_ohlcv,
                    instruments=instruments,
                    elapsed_minutes=elapsed_minutes,
                )
                if enriched_today.is_empty():
                    logger.warning("增量计算结果为空, 回退到全量计算")
                    use_incremental = False

            # ---- 全量回退路径 ----
            if not use_incremental:
                from datetime import timedelta
                from app.indicators.pipeline import compute_enriched

                logger.info("enriched 全量计算 (live_agg=%s, 上次日期=%s)",
                            "ok" if not live_agg.is_empty() else "空", prev_date)

                cutoff = today - timedelta(days=90)
                table = "kline_etf_daily" if asset_type == "etf" else "kline_daily"
                daily_glob = str(self._repo.store.data_dir / table / "**" / "*.parquet")
                ohlcv_cols = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "quote_ts"]
                hist_df = (
                    scan_daily_parquet(daily_glob)
                    .filter(pl.col("date") >= cutoff)
                    .sort(["symbol", "date"])
                    .collect()
                )
                if hist_df.is_empty():
                    return

                hist_cols = [c for c in ohlcv_cols if c in hist_df.columns]
                hist_df = hist_df.select(hist_cols).filter(pl.col("date") != today)
                daily_ohlcv = daily_df.select([c for c in ohlcv_cols if c in daily_df.columns])
                full_df = pl.concat([hist_df, daily_ohlcv], how="diagonal_relaxed")
                full_df = full_df.sort(["symbol", "date"])

                factor_dir = "adj_factor_etf" if asset_type == "etf" else "adj_factor"
                factor_path = self._repo.store.data_dir / factor_dir / "all.parquet"
                factors = pl.DataFrame()
                if factor_path.exists():
                    try:
                        factors = pl.read_parquet(factor_path)
                    except Exception:
                        pass
                instruments = self._repo.get_instruments() if asset_type == "stock" else None

                enriched_full = compute_enriched(full_df, factors=factors, instruments=instruments)
                enriched_today = enriched_full.filter(pl.col("date") == today)

            if enriched_today.is_empty():
                return

            # ---- 写盘 + 更新缓存 ----
            if merge:
                self._repo.merge_live_enriched_asset(asset_type, enriched_today)
            else:
                self._repo.flush_live_enriched_asset(asset_type, enriched_today)

            elapsed = time.perf_counter() - t0
            mode_label = "增量" if use_incremental else "全量"
            logger.info("enriched %s: %d 只, %s, 耗时 %.0fms",
                        mode_label, len(enriched_today), today, elapsed * 1000)
        except Exception as e:  # noqa: BLE001
            logger.warning("enriched 计算失败: %s", e)
