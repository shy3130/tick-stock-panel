"""ExternalFallbackAdapter — 能力门控 + 源选择 + 熔断 + 缓存 (P1: realtime)。

调用序 (resolve_realtime):
  1. preferences 门控: external_fallback_enabled & "realtime" ∈ scopes
  2. 交易日门控: 非交易日 (周末/节假日) 不触发网络
  3. 本地优先: 本地快照当日存在 → 返回 NOT_NEEDED (不触发网络)
  4. 本地缺失/陈旧 → 调 TencentQuoteSource (熔断/限速/单飞/缓存)
  5. 校准过滤 → 返回带 provenance 的 FallbackResult

绝不把结果交给 QuoteService / repository / enriched / monitor / screener / backtest。
"""
from __future__ import annotations

import enum
import logging
import threading
import typing
from dataclasses import dataclass, field
from datetime import date

from app.services.external_fallback.calibration import filter_valid_depth, filter_valid_rows
from app.services.external_fallback.circuit import CircuitBreaker
from app.services.external_fallback.sources.tencent_quote import (
    TencentQuoteSource,
    is_supported,
    to_exch_code,
    to_symbol,
)

logger = logging.getLogger(__name__)

# 批量上限 (受控 fallback 契约: snapshot 端点最多 60 个 symbol)。
MAX_SYMBOLS = 60


class Scope(str, enum.Enum):
    REALTIME = "realtime"
    DEPTH = "depth"


# 首批 scope 白名单 (契约: 仅 realtime/depth 子集)。
ALLOWED_SCOPES = frozenset({Scope.REALTIME.value, Scope.DEPTH.value})


class FallbackReason(str, enum.Enum):
    """fallback 触发原因。仅在实际命中外部源时出现于响应。"""

    LOCAL_SNAPSHOT_MISSING = "local_snapshot_missing"
    LOCAL_SNAPSHOT_STALE = "local_snapshot_stale"
    PROVIDER_NO_DEPTH = "provider_no_depth"


@dataclass
class DepthFallbackResult:
    """resolve_depth 的返回值。

    depth_map: {symbol: {bid_prices, bid_volumes, ask_prices, ask_volumes,
             timestamp, stale_session, source}} — 仅进程内缓存, 绝不落盘。
    used_fallback=True 时才应在 API 响应中追加 degraded/sources/fallback_reason。
    """

    depth_map: dict = field(default_factory=dict)
    used_fallback: bool = False
    source: str | None = None
    reason: FallbackReason | None = None


@dataclass
class FallbackResult:
    """resolve_realtime 的返回值。

    rows 为已校准的外部行情行 (source="tencent_quote"); 绝不进入持久化链路。
    used_fallback=True 时才应在 API 响应中追加 degraded/sources/fallback_reason。
    """

    rows: list[dict] = field(default_factory=list)
    used_fallback: bool = False
    source: str | None = None
    reason: FallbackReason | None = None


class ExternalFallbackAdapter:
    """单例适配器 (线程安全)。

    所有 HTTP/clock seam 经 TencentQuoteSource 注入; 本类只做门控与编排。
    """

    def __init__(
        self,
        *,
        tencent_source: TencentQuoteSource | None = None,
        clock_ns: typing.Callable[[], int] | None = None,
    ) -> None:
        self._tencent = tencent_source or TencentQuoteSource()
        self._lock = threading.Lock()
        # 连续口径校准失败计数 (契约 §4.6: 连续 3 次口径校验失败 → 熔断)。
        # 独立于网络层 record_failure, 因 _guarded_fetch 会在 HTTP 成功时重置网络失败计数。
        self._calibration_failures: dict[str, int] = {}
        self._calibration_threshold = 3

    # ---- 门控 -------------------------------------------------------------
    @staticmethod
    def _is_enabled_for_scope(scope: str) -> bool:
        """读 preferences: external_fallback_enabled & scope ∈ scopes。"""
        from app.services import preferences

        if not preferences.get_external_fallback_enabled():
            return False
        return scope in preferences.get_external_fallback_scopes()

    @staticmethod
    def _is_cn_trading_day(today: date | None = None) -> bool:
        """当前是否中国 A 股交易日 (简化: 周末排除; 节假日由本地快照存在性兜底)。

        契约: 非交易日不触发网络。节假日判定不在 P1 范围 (本地数据存在即走本地)。
        """
        d = today or _cn_today()
        return d.weekday() < 5  # 0-4 = Mon-Fri

    # ---- 本地快照新鲜度 ---------------------------------------------------
    @staticmethod
    def _local_snapshot_is_fresh(local_rows: list[dict]) -> bool:
        """本地 realtime 行是否覆盖当前中国交易日。

        判据: 任一行的 date/timestamp 等于当前交易日 (cn_today)。
        - timestamp (str): 取前 10 位 YYYY-MM-DD 比较
        - date (str): 直接比较
        local_rows 为空或全部日期 < 今日 → 视为陈旧/缺失。
        """
        if not local_rows:
            return False
        today = _cn_today_iso()
        for row in local_rows:
            ts = row.get("timestamp") or row.get("date")
            if ts and str(ts)[:10] == today:
                return True
        return False

    @staticmethod
    def _classify_reason(local_rows: list[dict]) -> FallbackReason:
        """本地缺失 (无行) vs 陈旧 (有行但日期 < 今日)。"""
        if not local_rows:
            return FallbackReason.LOCAL_SNAPSHOT_MISSING
        return FallbackReason.LOCAL_SNAPSHOT_STALE

    # ---- 主入口 -----------------------------------------------------------
    def resolve_realtime(
        self,
        symbols: list[str],
        local_rows: list[dict],
    ) -> FallbackResult:
        """本地优先 realtime resolver。

        symbols: 调用方规范化后的 symbol 列表 (上限 MAX_SYMBOLS 由调用方保证)。
        local_rows: 调用方从本地 (provider/QuoteService) 取出的行, 用于新鲜度判定。
        返回 FallbackResult; 绝不交给 repository / QuoteService 写入。
        """
        # 1. 门控: 关闭 / 无 scope → 零网络
        if not self._is_enabled_for_scope(Scope.REALTIME.value):
            return FallbackResult()
        # 2. 交易日门控
        if not self._is_cn_trading_day():
            logger.debug("external_fallback realtime skipped: non-trading day")
            return FallbackResult()
        # 3. 本地优先: 当日快照存在 → 不触发网络
        if self._local_snapshot_is_fresh(local_rows):
            return FallbackResult()
        # 4. 本地缺失/陈旧 → 调腾讯源
        reason = self._classify_reason(local_rows)
        clean_symbols = [s for s in symbols if is_supported(s)]
        if not clean_symbols:
            return FallbackResult()

        # 外部 adapter 边界: 内部 canonical .INDEX → 腾讯交易所代码映射后,
        # 腾讯返回的 symbol 是 .SH/.SZ 形式 → 需逆映射回调用方的 .INDEX。
        symbol_remap: dict[str, str] = {}
        for s in symbols:
            if s.endswith(".INDEX"):
                exch = to_exch_code(s)
                if exch:
                    tencent_form = to_symbol(exch)
                    if tencent_form and tencent_form != s:
                        symbol_remap[tencent_form] = s

        try:
            fetch_result = self._tencent.get_realtime_result(clean_symbols[:MAX_SYMBOLS])
        except Exception:
            # TencentQuoteSource 已自行负责传输失败计数; 此处不能再重复记账。
            logger.warning("external_fallback tencent get_realtime raised")
            return FallbackResult()
        rows = filter_valid_rows(fetch_result.rows)
        if not rows:
            if not fetch_result.all_requests_succeeded:
                # 网络失败或熔断短路不是口径失败, 既不双重记网络错误, 也不
                # 重置已开启的冷却窗口。
                logger.info("external_fallback realtime source unavailable")
                return FallbackResult()
            # 腾讯 HTTP 成功但返回全为口径无效行 → 连续 3 次熔断。
            logger.info(
                "external_fallback realtime fallback yielded 0 valid rows (requested=%d)",
                len(clean_symbols),
            )
            self._record_calibration_failure("tencent_quote")
            return FallbackResult()

        # 校准成功 → 重置连续口径失败计数
        self._reset_calibration_failures("tencent_quote")

        # 外部 adapter 边界: 把腾讯返回的 .SH/.SZ 指数 symbol 逆映射回 .INDEX
        if symbol_remap:
            for row in rows:
                sym = row.get("symbol")
                if sym and sym in symbol_remap:
                    row["symbol"] = symbol_remap[sym]

        logger.info(
            "external_fallback realtime fallback: %d rows, reason=%s",
            len(rows), reason.value,
        )
        return FallbackResult(
            rows=rows,
            used_fallback=True,
            source="tencent_quote",
            reason=reason,
        )

    def resolve_depth(
        self,
        symbols: list[str],
        has_local_depth: bool = False,
    ) -> DepthFallbackResult:
        """本地优先 depth resolver (镜像 resolve_realtime 的门控序)。

        has_local_depth: 调用方 (depth_service) 判定本地 provider 是否有 depth 能力。
        True → 不走外部 (provider 自给自足); False → 腾讯五档。
        返回 DepthFallbackResult; depth_map 仅进程内缓存, 绝不进入 repository/QuoteService。
        """
        # 1. 门控: 关闭 / 无 depth scope → 零网络
        if not self._is_enabled_for_scope(Scope.DEPTH.value):
            return DepthFallbackResult()
        # 2. 交易日门控
        if not self._is_cn_trading_day():
            logger.debug("external_fallback depth skipped: non-trading day")
            return DepthFallbackResult()
        # 3. 本地优先: provider 有 depth 能力 → 不走外部
        if has_local_depth:
            return DepthFallbackResult()
        # 4. provider 无能力 → 调腾讯五档源
        clean_symbols = [s for s in symbols if is_supported(s)]
        if not clean_symbols:
            return DepthFallbackResult()

        try:
            fetch_result = self._tencent.get_depth_result(clean_symbols[:MAX_SYMBOLS])
        except Exception:
            logger.warning("external_fallback tencent get_depth raised")
            return DepthFallbackResult()
        depth_map = filter_valid_depth(fetch_result.depth_map)
        if not depth_map:
            if not fetch_result.all_requests_succeeded:
                logger.info("external_fallback depth source unavailable")
                return DepthFallbackResult()
            logger.info(
                "external_fallback depth fallback yielded 0 valid rows (requested=%d)",
                len(clean_symbols),
            )
            self._record_calibration_failure("tencent_quote")
            return DepthFallbackResult()

        self._reset_calibration_failures("tencent_quote")
        logger.info("external_fallback depth fallback: %d symbols", len(depth_map))
        return DepthFallbackResult(
            depth_map=depth_map,
            used_fallback=True,
            source="tencent_quote",
            reason=FallbackReason.PROVIDER_NO_DEPTH,
        )

    def _record_calibration_failure(self, source: str) -> None:
        """连续口径校准失败达到阈值时强制熔断 (独立于网络层 record_success 重置)。"""
        with self._lock:
            n = self._calibration_failures.get(source, 0) + 1
            self._calibration_failures[source] = n
            opened = n >= self._calibration_threshold
        if opened:
            self._tencent._circuit.force_open(
                source, reason=f"{n} consecutive calibration failures"
            )

    def _reset_calibration_failures(self, source: str) -> None:
        """校准成功后清零连续口径失败计数。"""
        with self._lock:
            self._calibration_failures.pop(source, None)

    @staticmethod
    def get_circuit() -> CircuitBreaker | None:
        """暴露熔断器 (供测试/可观测性)。单例未构造时返回 None, 不抛异常。"""
        adapter = _DEFAULT_ADAPTER
        if adapter is None:
            return None
        return adapter._tencent._circuit


# ---- 单例 --------------------------------------------------------------------
# 适配器全局单例; 进程内复用缓存/单飞/熔断。测试通过构造新实例或注入 source 覆盖。
_DEFAULT_ADAPTER: ExternalFallbackAdapter | None = None
_DEFAULT_LOCK = threading.Lock()


def get_adapter() -> ExternalFallbackAdapter:
    """获取进程级单例 adapter。"""
    global _DEFAULT_ADAPTER
    if _DEFAULT_ADAPTER is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_ADAPTER is None:
                _DEFAULT_ADAPTER = ExternalFallbackAdapter()
    return _DEFAULT_ADAPTER


def reset_adapter(adapter: ExternalFallbackAdapter | None = None) -> None:
    """重置单例 (测试用; 生产路径不调用)。"""
    global _DEFAULT_ADAPTER
    with _DEFAULT_LOCK:
        _DEFAULT_ADAPTER = adapter


# ---- 时区辅助 ---------------------------------------------------------------
def _cn_today() -> date:
    """当前 Asia/Shanghai 日期 (UTC+8)。"""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone(timedelta(hours=8)))
    return now.date()


def _cn_today_iso() -> str:
    return _cn_today().isoformat()
