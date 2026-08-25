"""Tencent realtime quote source — 仅腾讯, 公共免费只读端点。

固定端点 (静态代码, 不可由调用方覆盖):
  host: qt.gtimg.cn  (host allowlist 强制)
  path: /q=<exchange_codes>
  batch 上限: 60 个 code / 请求

知识来源: backend/app/data_providers/fquant/sina_tencent_client.py 的腾讯解析逻辑,
重写为 service 侧独立适配器。FQuantProvider 内不保留任何外部网络调用。

稳健解码:
  - 腾讯响应为 GBK 编码文本, 容错尝试 GBK → UTF-8 → latin-1
  - 字段以 '~' 分隔的定长序; 字段位置契约见 TENCENT_FIELD_INDEX

单位口径 (展示响应契约):
  - volume: 股 (腾讯 cjl 是手, ×100)
  - amount: 元 (腾讯 cje 是万元, ×10000)
  - change_pct: 百分数 (1.23 = 1.23%) —— 腾讯原始即百分点
  - timestamp: Asia/Shanghai 时区 ISO 字符串

禁止泄露: 原始响应文本、请求 URL/参数/host 不进入日志或 API 响应。
"""
from __future__ import annotations

import logging
import random
import threading
import time
import typing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from app.services.external_fallback.circuit import CircuitBreaker

logger = logging.getLogger(__name__)

# ---- 固定端点 (静态) --------------------------------------------------------
TENCENT_HOST = "qt.gtimg.cn"
TENCENT_PATH = "/q="
TENCENT_URL = f"https://{TENCENT_HOST}{TENCENT_PATH}"
_BATCH_LIMIT = 60

_SHANGHAI_TZ = timezone(timedelta(hours=8))

# Host allowlist: 仅放行腾讯行情域名, 拒绝任何其它 host (含调用方注入的 url)。
_ALLOWED_HOSTS: frozenset[str] = frozenset({TENCENT_HOST})

# 可重试 HTTP status (瞬态过载/限流); 400/401/403/404 与 schema 失败不重试。
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 502, 503, 504})

# 最小请求间隔 (host 级限流, 受控 fallback 契约 §4.6 要求 ≥0.35s)。
_MIN_INTERVAL = 0.35
_TIMEOUT = 5.0
_MAX_RETRIES = 2
_CACHE_TTL = 2.0


@dataclass(frozen=True)
class RealtimeFetchResult:
    """一次腾讯 realtime 请求的传输结果。

    ``rows`` 为空但 ``all_requests_succeeded`` 为真时, 才代表源实际返回了
    无法通过解析/口径校准的内容; 网络失败、熔断短路与空请求不应被误计为
    口径校准失败。
    """

    rows: list[dict]
    all_requests_succeeded: bool


@dataclass(frozen=True)
class DepthFetchResult:
    """一次腾讯 depth 五档请求的传输结果。

    depth_map: {symbol: {bid_prices, bid_volumes, ask_prices, ask_volumes,
             timestamp, stale_session, source}} — 单位见 _parse_tencent_depth。
    all_requests_succeeded: 任一批网络失败时 False (口径失败由适配层独立计数)。
    """
    depth_map: dict[str, dict]
    all_requests_succeeded: bool

# ---- 腾讯字段位置契约 (定长序, '~' 分隔) -----------------------------------
# 参照 sina_tencent_client.parse_tencent 的已验证字段索引。
# 0: 未知标记, 1: 名称, 2: 代码, 3: 最新价, 4: 昨收, 5: 今开, 6: 成交量(手),
# 30: 时间日期(yyyymmdd), 31: 时间(hhmmss), 33: 最高, 34: 最低, 37: 成交额(万元)
_IDX_NAME = 1
_IDX_CODE = 2
_IDX_LAST = 3
_IDX_PREV = 4
_IDX_OPEN = 5
_IDX_VOL_HAND = 6
_IDX_DATE = 30
_IDX_TIME = 31
_IDX_HIGH = 33
_IDX_LOW = 34
_IDX_AMOUNT_WAN = 37

# 符号映射: 内部 symbol <-> 腾讯 exchange code (静态, 不走网络)。
_SUFFIX_TO_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk"}
_PREFIX_TO_SUFFIX = {"sh": "SH", "sz": "SZ", "bj": "BJ", "hk": "HK"}

# 支持的市场后缀白名单 (指数 .INDEX 也通过 code 前缀推导交易所)。
_SUPPORTED_SUFFIXES = frozenset({"SH", "SZ", "BJ", "HK", "INDEX"})


def _index_code_to_exchange(code: str) -> str | None:
    """指数 code → 交易所前缀 (sh/sz)。无法确定返回 None。

    399xxx → 深证系列 (sz); 000xxx/880xxx → 上证系列 (sh)。
    """
    if code.startswith("399"):
        return "sz"
    if code.startswith(("000", "880")):
        return "sh"
    return None


def to_exch_code(symbol: str) -> str | None:
    """内部 symbol → 腾讯 exchange code (如 sh600000)。非法返回 None。

    .INDEX 后缀按 code 前缀推导交易所 (000→sh, 399→sz)。
    """
    code, _, suffix = symbol.strip().upper().partition(".")
    if suffix == "INDEX":
        prefix = _index_code_to_exchange(code)
    else:
        prefix = _SUFFIX_TO_PREFIX.get(suffix)
    if prefix is None or not code:
        return None
    return f"{prefix}{code}"


def to_symbol(exch_code: str) -> str | None:
    """腾讯 exchange code → 内部 symbol (如 600000.SH)。非法返回 None。"""
    if len(exch_code) < 3:
        return None
    prefix = exch_code[:2].lower()
    suffix = _PREFIX_TO_SUFFIX.get(prefix)
    if suffix is None:
        return None
    return f"{exch_code[2:]}.{suffix}"


def is_supported(symbol: str) -> bool:
    """符号是否属于腾讯支持的市场。"""
    _, _, suffix = symbol.strip().upper().partition(".")
    return suffix in _SUPPORTED_SUFFIXES


def _float(value) -> float | None:
    """正数转 float; None/0/非法 → None。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _depth_int(value) -> int | None:
    """五档量解析: 非负 int (0 有效 = 封单检测不变量); 非法/负 → None。

    与 _float 的关键差异: 0 是合法有效值 (涨停价卖一为 0 = 真封板),
    不可被正数过滤丢弃。单位保持腾讯原始 (手)。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return int(number)


def _parse_tencent(text: str, *, now: datetime | None = None) -> list[dict]:
    """解析腾讯行情文本为内部行。

    返回行单位口径已校准: volume=股, amount=元, change_pct=百分点,
    timestamp=Asia/Shanghai ISO。不包含任何原始响应片段。
    """
    rows: list[dict] = []
    for line in text.split(";"):
        if "=" not in line:
            continue
        key, _, payload = line.partition("=")
        exch_code = key.split("v_")[-1].strip()
        symbol = to_symbol(exch_code)
        if symbol is None:
            continue
        parts = payload.strip().strip('"').split("~")
        if len(parts) <= _IDX_VOL_HAND:
            continue

        last_price = _float(parts[_IDX_LAST])
        prev_close = _float(parts[_IDX_PREV])
        open_ = _float(parts[_IDX_OPEN])
        vol_hand = _float(parts[_IDX_VOL_HAND])
        volume = vol_hand * 100 if vol_hand is not None else None
        high = _float(parts[_IDX_HIGH]) if len(parts) > _IDX_HIGH else None
        low = _float(parts[_IDX_LOW]) if len(parts) > _IDX_LOW else None
        amount = None
        if len(parts) > _IDX_AMOUNT_WAN:
            wan = _float(parts[_IDX_AMOUNT_WAN])
            amount = wan * 10000 if wan is not None else None

        change_amount = None
        change_pct = None
        if last_price is not None and prev_close not in (None, 0):
            change_amount = last_price - prev_close
            change_pct = change_amount / prev_close * 100

        timestamp = _build_shanghai_timestamp(
            parts[_IDX_DATE] if len(parts) > _IDX_DATE else None,
            parts[_IDX_TIME] if len(parts) > _IDX_TIME else None,
        )

        rows.append({
            "symbol": symbol,
            "name": parts[_IDX_NAME] if len(parts) > _IDX_NAME else None,
            "last_price": last_price,
            "prev_close": prev_close,
            "open": open_,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "change_pct": change_pct,
            "change_amount": change_amount,
            "timestamp": timestamp,
            "stale_session": not _is_market_session_open(symbol, now=now),
            "source": "tencent_quote",
        })
    return rows


# 五档盘口字段索引 (~ 分隔后; 参照 sina_tencent_client.parse_tencent_depth):
#   bid_prices[i] = parts[9 + i*2],  bid_volumes[i] = parts[10 + i*2]  (i=0..4)
#   ask_prices[i] = parts[19 + i*2], ask_volumes[i] = parts[20 + i*2]  (i=0..4)
# 解析所需最小 parts 长度: 29 (覆盖 ask_volumes[4] = parts[28])。
_DEPTH_MIN_PARTS = 29


def _parse_tencent_depth(text: str, *, now: datetime | None = None) -> dict[str, dict]:
    """解析腾讯行情文本为五档 depth_map。

    返回 {symbol: {bid_prices, bid_volumes, ask_prices, ask_volumes,
             timestamp, stale_session, source}}。
    单位口径: bid/ask prices=元 (正数 float|None), bid/ask volumes=手 (非负 int|None;
    0 有效 = 封单检测不变量)。timestamp=Asia/Shanghai ISO。不包含原始响应片段。
    """
    depth_map: dict[str, dict] = {}
    for line in text.split(";"):
        if "=" not in line:
            continue
        key, _, payload = line.partition("=")
        exch_code = key.split("v_")[-1].strip()
        symbol = to_symbol(exch_code)
        if symbol is None:
            continue
        parts = payload.strip().strip('"').split("~")
        if len(parts) < _DEPTH_MIN_PARTS:
            continue

        bid_prices: list[float | None] = []
        bid_volumes: list[int | None] = []
        ask_prices: list[float | None] = []
        ask_volumes: list[int | None] = []
        for i in range(5):
            bid_prices.append(_float(parts[9 + i * 2]))
            bid_volumes.append(_depth_int(parts[10 + i * 2]))
            ask_prices.append(_float(parts[19 + i * 2]))
            ask_volumes.append(_depth_int(parts[20 + i * 2]))

        timestamp = _build_shanghai_timestamp(
            parts[_IDX_DATE] if len(parts) > _IDX_DATE else None,
            parts[_IDX_TIME] if len(parts) > _IDX_TIME else None,
        )
        depth_map[symbol] = {
            "bid_prices": bid_prices,
            "bid_volumes": bid_volumes,
            "ask_prices": ask_prices,
            "ask_volumes": ask_volumes,
            "timestamp": timestamp,
            "stale_session": not _is_market_session_open(symbol, now=now),
            "source": "tencent_quote",
        }
    return depth_map


def _build_shanghai_timestamp(date_str: str | None, time_str: str | None) -> str | None:
    """腾讯 yyyymmdd + hhmmss(可能带毫秒) → Asia/Shanghai ISO 字符串。

    返回 'YYYY-MM-DDTHH:MM:SS+08:00'。无法解析时返回 None。
    """
    if not date_str or len(str(date_str).strip()) < 8:
        return None
    ds = str(date_str).strip()
    try:
        y, m, d = int(ds[:4]), int(ds[4:6]), int(ds[6:8])
    except ValueError:
        return None
    hh = mm = ss = 0
    ts = str(time_str or "").strip()
    if len(ts) >= 6:
        try:
            hh = int(ts[:2])
            mm = int(ts[2:4])
            ss = int(ts[4:6])
        except ValueError:
            hh = mm = ss = 0
    try:
        dt = datetime(y, m, d, hh, mm, ss, tzinfo=_SHANGHAI_TZ)
    except ValueError:
        return None
    return dt.isoformat()


def _is_market_session_open(symbol: str, *, now: datetime | None = None) -> bool:
    """返回该标的市场当前是否处于可交易时段。

    行情源在闭市时仍可能返回最近一次快照; 该快照可展示但必须显式标为
    ``stale_session``, 不能伪装成盘中报价。时段定义复用 ``app.markets``。
    """
    from app.markets import market_of

    current = (now or datetime.now(_SHANGHAI_TZ)).astimezone(_SHANGHAI_TZ)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return any(
        start <= current_time <= end
        for start, end in market_of(symbol).sessions
    )


def decode_response(raw: bytes) -> str:
    """腾讯响应稳健解码: GBK → UTF-8 → latin-1 (兜底, 不抛异常)。

    GBK 是腾讯已知编码; UTF-8 是部分指数端点的偶发编码; latin-1 保证字节无损。
    """
    for enc in ("gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


class TencentQuoteSource:
    """腾讯公共行情源 — 受控 HTTP + 单飞 + 短 TTL 缓存 + 熔断。

    所有网络参数 (host/path/timeout/间隔) 均为模块常量, 调用方不可覆盖。
    clock/sleeper/rng/http_getter 可注入, 用于边界测试。
    """

    SOURCE_NAME = "tencent_quote"

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
        # 生产默认: 固定 trust_env=False 的 Client, 杜绝代理/环境变量串入。
        # 注入 getter 仅用于测试; 生产 Client.get 本身不接受 trust_env 参数。
        self._client = httpx.Client(trust_env=False, timeout=_TIMEOUT)
        self._http_getter = http_getter
        # 每 host 限流: last request monotonic
        self._last_request = 0.0
        # 短 TTL 缓存: url → (text, expires_at)
        self._cache: dict[str, tuple[str, float]] = {}
        # single-flight: url → {"event", "result"}
        self._inflight: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---- Host allowlist ---------------------------------------------------
    @staticmethod
    def _is_allowed(url: str) -> bool:
        host = urlparse(url).hostname
        if host not in _ALLOWED_HOSTS:
            logger.warning("external_fallback rejected non-allowlisted host: %s", host)
            return False
        return True

    # ---- 缓存 -------------------------------------------------------------
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

    # ---- 限流 -------------------------------------------------------------
    def _enforce_rate_limit(self) -> None:
        now = self._clock()
        wait = _MIN_INTERVAL - (now - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()

    # ---- 错误分类 ---------------------------------------------------------
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

    # ---- 带重试的拉取 -----------------------------------------------------
    def _fetch_with_retry(self, url: str) -> str | None:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                if self._http_getter is None:
                    resp = self._client.get(url, timeout=_TIMEOUT)
                else:
                    resp = self._http_getter(url, timeout=_TIMEOUT, trust_env=False)
                resp.raise_for_status()
                text = decode_response(resp.content)
                if not text or not text.strip():
                    logger.warning("external_fallback tencent empty response, not retrying")
                    return None
                return text
            except Exception as exc:  # noqa: BLE001
                if attempt < _MAX_RETRIES and self._is_transient(exc):
                    self._backoff_sleep(attempt)
                    continue
                logger.warning("external_fallback tencent GET failed (attempt %d)", attempt + 1)
                return None
        return None

    def _guarded_fetch(self, url: str) -> str | None:
        if not self._circuit.source_available(self.SOURCE_NAME):
            return None
        self._enforce_rate_limit()
        text = self._fetch_with_retry(url)
        if text is None:
            self._circuit.record_failure(self.SOURCE_NAME)
            return None
        self._circuit.record_success(self.SOURCE_NAME)
        self._cache_put(url, text)
        return text

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
        """受控拉取入口: allowlist → 缓存 → single-flight → 熔断 → 限流 → 重试。"""
        if not self._is_allowed(url):
            return None
        cached = self._cache_get(url)
        if cached is not None:
            return cached
        holder = self._register_inflight(url)
        if holder is None:
            result = self._guarded_fetch(url)
            self._complete_inflight(url, result)
            return result
        holder["event"].wait()
        return holder["result"]

    def get_realtime(self, symbols: list[str]) -> list[dict]:
        """批量拉取腾讯实时快照, 保留面向调用方的既有 rows 返回契约。"""
        return self.get_realtime_result(symbols).rows

    def get_realtime_result(self, symbols: list[str]) -> RealtimeFetchResult:
        """批量拉取并携带传输成功语义, 供适配层区分网络与校准失败。

        单个 snapshot 请求最多 60 个标的; 即使未来调用方扩展为多批, 只要有
        任一批网络失败, 整次调用都不能被归类为「源返回全量口径无效」。
        """
        codes: list[str] = []
        for s in symbols:
            code = to_exch_code(s)
            if code:
                codes.append(code)
        if not codes:
            return RealtimeFetchResult(rows=[], all_requests_succeeded=False)

        rows: list[dict] = []
        all_requests_succeeded = True
        for start in range(0, len(codes), _BATCH_LIMIT):
            chunk = codes[start:start + _BATCH_LIMIT]
            text = self._http_get(TENCENT_URL + ",".join(chunk))
            if text is None:
                all_requests_succeeded = False
                continue
            rows.extend(_parse_tencent(text))
        return RealtimeFetchResult(
            rows=rows,
            all_requests_succeeded=all_requests_succeeded,
        )

    def get_depth_result(self, symbols: list[str]) -> DepthFetchResult:
        """批量拉取腾讯五档盘口, 返回 DepthFetchResult。

        复用 _http_get (allowlist → 缓存 → single-flight → 熔断 → 限流 → 重试),
        同 symbol 的 realtime + depth 请求自动被 single-flight + 缓存去重。
        """
        codes: list[str] = []
        for s in symbols:
            code = to_exch_code(s)
            if code:
                codes.append(code)
        if not codes:
            return DepthFetchResult(depth_map={}, all_requests_succeeded=False)

        depth_map: dict[str, dict] = {}
        all_requests_succeeded = True
        for start in range(0, len(codes), _BATCH_LIMIT):
            chunk = codes[start:start + _BATCH_LIMIT]
            text = self._http_get(TENCENT_URL + ",".join(chunk))
            if text is None:
                all_requests_succeeded = False
                continue
            depth_map.update(_parse_tencent_depth(text))
        return DepthFetchResult(
            depth_map=depth_map,
            all_requests_succeeded=all_requests_succeeded,
        )
