"""Tencent intraday minute-chart source — 仅腾讯分时端点, 公共免费只读。

受控 chart_live fallback 专用 (契约 §4): 仅当前 CN 交易日、A 股单标的
(SH/SZ/BJ)、本地当日分钟/日K缺失时, 拉取腾讯分时数据做纯展示兜底。

固定端点 (静态, 调用方不可覆盖):
  https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=<exchCode>

payload 结构 (实测):
  {"code": 0, "data": {"<exchCode>": {"data": {"date": "YYYYMMDD",
   "data": ["HHMM price cum_volume cum_amount", "HHMM ..."]}}}}

每行四个字段: 时间(HHMM) 价格(元) 累计成交量(手) 累计成交额(元)。
必须换算为当前日期的每分钟增量行; 拒绝日期不匹配、午休/盘中时段外、解析失败
或累计倒退。全部行 source="tencent_chart" + provisional=True, 绝不落盘。
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
import typing
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

import httpx

from app.services.external_fallback.circuit import CircuitBreaker

logger = logging.getLogger(__name__)

# ---- 固定端点 (静态) --------------------------------------------------------
TENCENT_CHART_HOST = "web.ifzq.gtimg.cn"
TENCENT_CHART_PATH = "/appstock/app/minute/query"
TENCENT_CHART_URL = f"https://{TENCENT_CHART_HOST}{TENCENT_CHART_PATH}"

# Host allowlist: 仅放行腾讯分时域名, 拒绝任何其它 host (含注入的 url)。
_ALLOWED_HOSTS: frozenset[str] = frozenset({TENCENT_CHART_HOST})

_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 502, 503, 504})
_MIN_INTERVAL = 0.35
_TIMEOUT = 5.0
_MAX_RETRIES = 2
_CACHE_TTL = 5.0

SOURCE_NAME = "tencent_chart"

# 分时行覆盖的盘中时间窗 (A 股): 09:15-11:30、13:00-15:05，
# 覆盖集合竞价与收盘竞价尾差，但拒绝午休伪数据。
_MORNING_SESSION_START_MIN = 9 * 60 + 15
_MORNING_SESSION_END_MIN = 11 * 60 + 30
_AFTERNOON_SESSION_START_MIN = 13 * 60
_AFTERNOON_SESSION_END_MIN = 15 * 60 + 5

# A 股市场后缀白名单 (指数/港股不支持 — chart_live 只兜 A 股个股图表)。
_A_SHARE_SUFFIXES = frozenset({"SH", "SZ", "BJ"})


def is_a_share_supported(symbol: str) -> bool:
    """symbol 是否 A 股个股 (SH/SZ/BJ 后缀; 不含 .INDEX)。"""
    _, _, suffix = symbol.strip().upper().partition(".")
    return suffix in _A_SHARE_SUFFIXES

def _in_a_share_session(minutes_of_day: int) -> bool:
    return (
        _MORNING_SESSION_START_MIN <= minutes_of_day <= _MORNING_SESSION_END_MIN
        or _AFTERNOON_SESSION_START_MIN <= minutes_of_day <= _AFTERNOON_SESSION_END_MIN
    )


def to_exch_code(symbol: str) -> str | None:
    """内部 symbol (600519.SH) → 腾讯分时 code (sh600519)。非 A 股返回 None。"""
    if not is_a_share_supported(symbol):
        return None
    code, _, suffix = symbol.strip().upper().partition(".")
    if not code.isdigit():
        return None
    return f"{suffix.lower()}{code}"


@dataclass(frozen=True)
class ChartMinuteRow:
    """统一 1m 增量行 (纯展示, 绝不写入 provider/repository)。"""

    datetime: str          # "YYYY-MM-DD HH:MM:00"
    time: str              # "HH:MM"
    open: float
    high: float
    low: float
    close: float
    volume: float          # 本分钟增量成交量 (股)
    amount: float          # 本分钟增量成交额 (元)
    source: str = SOURCE_NAME
    provisional: bool = True

    def as_dict(self) -> dict:
        return {
            "datetime": self.datetime,
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "source": self.source,
            "provisional": self.provisional,
        }


@dataclass(frozen=True)
class ChartDailyBar:
    """同源派生的当日临时日K bar (provisional, 绝不与本地 adjusted history 混写)。"""

    date: str              # "YYYY-MM-DD"
    open: float
    high: float
    low: float
    close: float
    volume: float          # 全天累计成交量 (股)
    amount: float          # 全天累计成交额 (元)
    source: str = SOURCE_NAME
    provisional: bool = True
    is_live: bool = True

    def as_dict(self) -> dict:
        return {
            "date": self.date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "source": self.source,
            "provisional": self.provisional,
            "is_live": self.is_live,
        }


@dataclass(frozen=True)
class ChartFetchResult:
    """一次腾讯分时请求的结果。

    minutes: 每分钟增量行 (source=tencent_chart, provisional=True)。
    daily:   同源派生的当日 provisional bar。
    transport_succeeded: HTTP 是否成功 (区分网络失败 vs 口径拒绝)。
    """

    minutes: list[dict] = field(default_factory=list)
    daily: dict | None = None
    transport_succeeded: bool = False


def _parse_minute_payload(
    text: str,
    *,
    exch_code: str,
    trade_date: date,
) -> tuple[list[ChartMinuteRow], ChartDailyBar]:
    """解析腾讯分时 JSON 并换算为增量行 + 当日 bar。

    拒绝: 日期不匹配、字段缺失/非法、累计倒退或时序倒退。
    午休、盘后附加行不属于盘中图，逐行丢弃；其余任一拒绝抛 ValueError
    (由调用方映射为未命中, 绝不部分返回)。
    """
    import json

    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError("minute payload is not json") from exc

    node = ((payload or {}).get("data") or {}).get(exch_code) or {}
    inner = node.get("data") or {}
    date_str = str(inner.get("date") or "")
    raw_lines = inner.get("data")

    if len(date_str) != 8 or not date_str.isdigit():
        raise ValueError("minute payload missing date")
    if date_str != trade_date.strftime("%Y%m%d"):
        raise ValueError("minute payload date mismatch")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ValueError("minute payload missing records")

    date_iso = trade_date.isoformat()
    prev_cum_vol: float | None = None
    prev_cum_amt: float | None = None
    prev_hhmm: int | None = None
    rows: list[ChartMinuteRow] = []
    for line in raw_lines:
        if not isinstance(line, str):
            raise ValueError("minute record is not text")
        parts = line.split()
        if len(parts) != 4:
            raise ValueError("minute line field count mismatch")
        hhmm, price_s, cum_vol_s, cum_amt_s = parts
        if len(hhmm) != 4 or not hhmm.isdigit():
            raise ValueError("minute time invalid")
        minutes_of_day = int(hhmm[:2]) * 60 + int(hhmm[2:])
        if prev_hhmm is not None and minutes_of_day < prev_hhmm:
            raise ValueError("minute time regressed")
        prev_hhmm = minutes_of_day
        # 腾讯在盘后可能追加延迟/收盘后快照行；这些行不属于当日盘中图，
        # 直接跳过而不使整个有效载荷失效。午休行同理。
        if not _in_a_share_session(minutes_of_day):
            continue

        try:
            price = float(price_s)
            cum_vol = float(cum_vol_s)
            cum_amt = float(cum_amt_s)
        except (ValueError, TypeError) as exc:
            raise ValueError("minute numeric field invalid") from exc
        if (
            not all(math.isfinite(value) for value in (price, cum_vol, cum_amt))
            or price <= 0
            or cum_vol < 0
            or cum_amt < 0
        ):
            raise ValueError("minute numeric field out of range")

        # 累计倒退拒绝 (契约: 量/额必须单调不减)
        if prev_cum_vol is not None and cum_vol < prev_cum_vol:
            raise ValueError("cumulative volume regressed")
        if prev_cum_amt is not None and cum_amt < prev_cum_amt:
            raise ValueError("cumulative amount regressed")

        # 手 → 股；成交额原始单位为元（实测需与 price*volume 同量级）。
        delta_vol = (cum_vol - (prev_cum_vol or 0.0)) * 100.0
        delta_amt = cum_amt - (prev_cum_amt or 0.0)
        prev_cum_vol, prev_cum_amt = cum_vol, cum_amt

        rows.append(
            ChartMinuteRow(
                datetime=f"{date_iso} {hhmm[:2]}:{hhmm[2:]}:00",
                time=f"{hhmm[:2]}:{hhmm[2:]}",
                open=price,
                high=price,
                low=price,
                close=price,
                volume=delta_vol,
                amount=delta_amt,
            )
        )

    if not rows:
        raise ValueError("minute payload no rows")

    daily = ChartDailyBar(
        date=date_iso,
        open=rows[0].close,
        high=max(r.high for r in rows),
        low=min(r.low for r in rows),
        close=rows[-1].close,
        volume=sum(r.volume for r in rows),
        amount=sum(r.amount for r in rows),
    )
    return rows, daily


class TencentChartSource:
    """腾讯分时源 — 受控 HTTP + 单飞 + 短 TTL 缓存 + 熔断 (镜像 TencentQuoteSource)。

    所有网络参数 (host/path/timeout/间隔) 均为模块常量, 调用方不可覆盖。
    clock/sleeper/rng/http_getter 可注入, 用于边界测试。
    绝不记录原始响应或 URL 到日志。
    """

    SOURCE_NAME = SOURCE_NAME

    def __init__(
        self,
        *,
        circuit: CircuitBreaker | None = None,
        clock: typing.Callable[[], float] | None = None,
        sleeper: typing.Callable[[float], None] | None = None,
        rng: typing.Callable[[], float] | None = None,
        http_getter: typing.Callable[..., httpx.Response] | None = None,
    ) -> None:
        self._circuit = circuit or CircuitBreaker()
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._rng = rng or random.random
        self._client = httpx.Client(trust_env=False, timeout=_TIMEOUT)
        self._http_getter = http_getter
        self._last_request = 0.0
        self._cache: dict[str, tuple[str, float]] = {}
        self._inflight: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._calibration_failures = 0

    def _record_calibration_failure(self) -> None:
        with self._lock:
            self._calibration_failures += 1
            failures = self._calibration_failures
        if failures >= 3:
            self._circuit.force_open(
                self.SOURCE_NAME,
                reason=f"{failures} consecutive calibration failures",
            )

    def _reset_calibration_failures(self) -> None:
        with self._lock:
            self._calibration_failures = 0

    # ---- Host allowlist ---------------------------------------------------
    @staticmethod
    def _is_allowed(url: str) -> bool:
        host = urlparse(url).hostname
        if host not in _ALLOWED_HOSTS:
            logger.warning(
                "external_fallback chart rejected non-allowlisted host: %s", host
            )
            return False
        return True

    # ---- 缓存 / 限流 / 重试 (与 tencent_quote 相同骨架) --------------------
    def _cache_get(self, url: str) -> str | None:
        entry = self._cache.get(url)
        if entry is None:
            return None
        text, expires = entry
        if self._clock() >= expires:
            self._cache.pop(url, None)
            return None
        return text

    def _cache_put(self, url: str, text: str) -> None:
        self._cache[url] = (text, self._clock() + _CACHE_TTL)

    def _enforce_rate_limit(self) -> None:
        now = self._clock()
        wait = _MIN_INTERVAL - (now - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()

    @staticmethod
    def _is_transient(exc: BaseException) -> bool:
        if isinstance(exc, httpx.TimeoutException):
            return True
        if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.NetworkError, httpx.RemoteProtocolError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in _RETRYABLE_STATUS
        return False

    def _backoff_sleep(self, attempt: int) -> None:
        delay = min(4.0, 0.3 * (2 ** attempt))
        jitter = self._rng() * delay
        if jitter > 0:
            self._sleep(jitter)

    def _fetch_with_retry(self, url: str) -> str | None:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                if self._http_getter is None:
                    resp = self._client.get(url, timeout=_TIMEOUT)
                else:
                    resp = self._http_getter(url, timeout=_TIMEOUT, trust_env=False)
                resp.raise_for_status()
                text = resp.content.decode("utf-8", errors="replace")
                if not text or not text.strip():
                    logger.warning("external_fallback chart empty response, not retrying")
                    return None
                return text
            except Exception as exc:  # noqa: BLE001
                if attempt < _MAX_RETRIES and self._is_transient(exc):
                    self._backoff_sleep(attempt)
                    continue
                logger.warning("external_fallback chart GET failed (attempt %d)", attempt + 1)
                return None
        return None

    # ---- single-flight ----------------------------------------------------
    def _register_inflight(self, url: str) -> dict | None:
        with self._lock:
            existing = self._inflight.get(url)
            if existing is not None:
                return existing
            self._inflight[url] = {"event": threading.Event(), "result": None}
            return None

    def _complete_inflight(self, url: str, result: str | None) -> None:
        with self._lock:
            holder = self._inflight.pop(url, None)
        if holder is not None:
            holder["result"] = result
            holder["event"].set()

    def _http_get(self, url: str) -> str | None:
        """受控拉取入口: 熔断 → allowlist → 缓存 → single-flight → 限流 → 重试。"""
        if not self._circuit.source_available(self.SOURCE_NAME):
            return None
        if not self._is_allowed(url):
            return None
        cached = self._cache_get(url)
        if cached is not None:
            return cached
        holder = self._register_inflight(url)
        if holder is None:
            self._enforce_rate_limit()
            text = self._fetch_with_retry(url)
            if text is None:
                self._circuit.record_failure(self.SOURCE_NAME)
            else:
                self._circuit.record_success(self.SOURCE_NAME)
                self._cache_put(url, text)
            self._complete_inflight(url, text)
            return text
        holder["event"].wait()
        return holder["result"]

    # ---- 主入口 -----------------------------------------------------------
    def get_minute_chart(self, symbol: str, trade_date: date) -> ChartFetchResult:
        """拉取单标的当前日分时并换算增量行 + provisional bar。

        任何失败 (网络/熔断/日期不匹配/解析/累计倒退) → ChartFetchResult 空
        结果, 绝不抛出、绝不部分返回。
        """
        exch_code = to_exch_code(symbol)
        if not exch_code:
            return ChartFetchResult()
        url = f"{TENCENT_CHART_URL}?code={exch_code}"
        text = self._http_get(url)
        if text is None:
            return ChartFetchResult()
        try:
            rows, daily = _parse_minute_payload(
                text, exch_code=exch_code, trade_date=trade_date
            )
        except ValueError as exc:
            # 口径校准失败独立于网络失败；连续三次立即熔断。
            # 日志不含 URL 或原始响应。
            logger.warning(
                "external_fallback chart payload rejected (%s %s): %s",
                symbol, trade_date.isoformat(), exc,
            )
            self._record_calibration_failure()
            return ChartFetchResult()
        self._reset_calibration_failures()
        return ChartFetchResult(
            minutes=[r.as_dict() for r in rows],
            daily=daily.as_dict(),
            transport_succeeded=True,
        )
