"""Sina/Tencent batch realtime quote client.

Outputs plain rows that are normalized by ``normalizer.normalize_realtime``.
Units: price=CNY, volume=shares, amount=CNY, change_pct=percentage points.
"""
from __future__ import annotations

import logging
import random
import threading
import time
import typing
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

TENCENT_URL = "https://qt.gtimg.cn/q="
SINA_URL = "https://hq.sinajs.cn/list="
TENCENT_BATCH = 60
SINA_BATCH = 100

# ---- M19 受控 HTTP 可靠性参数 -----------------------------------------------
# Host allowlist: 仅允许腾讯/新浪两个已知行情域名, 拒绝任何其它 Host。
_ALLOWED_HOSTS: frozenset[str] = frozenset({
    urlparse(TENCENT_URL).hostname,
    urlparse(SINA_URL).hostname,
})
# 可重试 HTTP status (瞬态过载/限流); 400/401/403/404 与 schema 失败不重试。
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 502, 503, 504})

_SUFFIX_TO_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk"}


def _to_exch_code(symbol: str) -> str:
    code, _, suffix = symbol.strip().upper().partition(".")
    return f"{_SUFFIX_TO_PREFIX.get(suffix, 'sh')}{code}"


def _to_symbol(exch_code: str) -> str:
    prefix = exch_code[:2].lower()
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ", "hk": "HK"}.get(prefix, prefix.upper())
    return f"{exch_code[2:]}.{suffix}"


def _f(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _n(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_tencent(text: str) -> list[dict]:
    rows: list[dict] = []
    for line in text.strip().split(";"):
        if "=" not in line:
            continue
        key, _, payload = line.partition("=")
        exch_code = key.split("v_")[-1].strip()
        parts = payload.strip().strip('"').split("~")
        if len(parts) < 7:
            continue
        vol_hand = _f(parts[6])
        row = {
            "symbol": _to_symbol(exch_code),
            "name": parts[1] if len(parts) > 1 else None,
            "last_price": _f(parts[3]) if len(parts) > 3 else None,
            "prev_close": _f(parts[4]) if len(parts) > 4 else None,
            "open": _f(parts[5]) if len(parts) > 5 else None,
            "volume": vol_hand * 100 if vol_hand is not None else None,
            "timestamp": None,
            "source": "tencent",
            "ext": {},
        }
        if len(parts) > 33:
            row["high"] = _f(parts[33])
        if len(parts) > 34:
            row["low"] = _f(parts[34])
        if len(parts) > 37:
            amount_10k = _f(parts[37])
            row["amount"] = amount_10k * 10000 if amount_10k is not None else None
        rows.append(row)
    return rows


def parse_tencent_depth(text: str) -> dict:
    rows: dict = {}
    for line in text.strip().split(";"):
        if "=" not in line:
            continue
        key, _, payload = line.partition("=")
        exch_code = key.split("v_")[-1].strip()
        parts = payload.strip().strip('"').split("~")
        if len(parts) < 29:
            continue

        bid_prices: list[float | None] = []
        bid_volumes: list[int | None] = []
        ask_prices: list[float | None] = []
        ask_volumes: list[int | None] = []
        for i in range(5):
            bid_prices.append(_n(parts[9 + i * 2]))
            bid_vol = _n(parts[10 + i * 2])
            bid_volumes.append(int(bid_vol) if bid_vol is not None else None)
            ask_prices.append(_n(parts[19 + i * 2]))
            ask_vol = _n(parts[20 + i * 2])
            ask_volumes.append(int(ask_vol) if ask_vol is not None else None)

        rows[_to_symbol(exch_code)] = {
            "bid_prices": bid_prices,
            "bid_volumes": bid_volumes,
            "ask_prices": ask_prices,
            "ask_volumes": ask_volumes,
            "timestamp": None,
            "source": "tencent",
        }
    return rows


def parse_sina(text: str, exch_codes: list[str] | None = None) -> list[dict]:  # noqa: ARG001
    rows: list[dict] = []
    for line in text.strip().split(";"):
        if "hq_str_" not in line:
            continue
        key, _, payload = line.partition("=")
        exch_code = key.split("hq_str_")[-1].strip()
        parts = payload.strip().strip('"').split(",")
        if len(parts) < 10:
            continue
        row = {
            "symbol": _to_symbol(exch_code),
            "name": parts[0],
            "open": _f(parts[1]),
            "prev_close": _f(parts[2]),
            "last_price": _f(parts[3]),
            "high": _f(parts[4]),
            "low": _f(parts[5]),
            "volume": _f(parts[8]),
            "amount": _f(parts[9]),
            "timestamp": None,
            "source": "sina",
            "ext": {},
        }
        if len(parts) > 31 and parts[30] and parts[31]:
            row["timestamp"] = f"{parts[30]}T{parts[31]}"
        rows.append(row)
    return rows


class SinaTencentClient:
    """腾讯/新浪实时行情批量客户端。

    受控 HTTP 可靠性 (M19):
      - Host allowlist: 仅放行 ``qt.gtimg.cn`` / ``hq.sinajs.cn``。
      - 每 Host 最小请求间隔 (限流)。
      - 短 TTL 响应缓存。
      - 同 URL single-flight (线程级去重, 防并发重复拉取)。
      - 瞬态错误分类重试 (timeout / connect reset / 429 / 502-504),
        非瞬态 (400/401/403/404 / schema 空) 不重试。
      - 带 jitter 的有界指数退避。
      - 连续失败熔断 + 恢复日志。
      - clock/sleep/random/http_getter 可注入, 便于快速测试。
    """

    def __init__(
        self,
        timeout: float = 4.0,
        *,
        max_retries: int = 2,
        min_interval: float = 0.2,
        cache_ttl: float = 1.0,
        circuit_threshold: int = 3,
        circuit_cooldown: float = 60.0,
        backoff_base: float = 0.3,
        backoff_cap: float = 4.0,
        clock: typing.Callable[[], float] | None = None,
        sleeper: typing.Callable[[float], None] | None = None,
        rng: typing.Callable[[], float] | None = None,
        http_getter: typing.Callable[..., object] | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = min_interval
        self.cache_ttl = cache_ttl
        self.circuit_threshold = circuit_threshold
        self.circuit_cooldown = circuit_cooldown
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._rng = rng or random.random
        self._http_getter = http_getter or httpx.get

        # 熔断状态
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        # 每 Host 限流: source -> 上次请求时刻
        self._last_request: dict[str, float] = {}
        # 短 TTL 响应缓存: url -> (text, expires_at)
        self._cache: dict[str, tuple[str, float]] = {}
        # single-flight: url -> {"event", "result"}
        self._inflight: dict[str, dict] = {}
        self._inflight_lock = threading.Lock()

    # ---- Host allowlist ---------------------------------------------------
    def _is_allowed_host(self, url: str) -> bool:
        host = urlparse(url).hostname
        if host not in _ALLOWED_HOSTS:
            logger.warning("rejected quote request to non-allowlisted host: %s", host)
            return False
        return True

    # ---- 响应缓存 ---------------------------------------------------------
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
        self._cache[url] = (text, self._clock() + self.cache_ttl)

    # ---- 每 Host 限流 -----------------------------------------------------
    def _enforce_rate_limit(self, source: str) -> None:
        last = self._last_request.get(source)
        now = self._clock()
        if last is not None:
            wait = self.min_interval - (now - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[source] = self._clock()

    # ---- 熔断 -------------------------------------------------------------
    def _source_available(self, source: str) -> bool:
        until = self._cooldown_until.get(source, 0)
        if until > self._clock():
            logger.debug("%s quote source cooling down for %.1fs", source, until - self._clock())
            return False
        return True

    def _record_success(self, source: str) -> None:
        was_open = source in self._cooldown_until
        self._failures[source] = 0
        self._cooldown_until.pop(source, None)
        if was_open:
            logger.info("%s quote source recovered, circuit closed", source)

    def _record_failure(self, source: str) -> None:
        failures = self._failures.get(source, 0) + 1
        self._failures[source] = failures
        if failures >= self.circuit_threshold:
            self._cooldown_until[source] = self._clock() + self.circuit_cooldown
            logger.warning(
                "%s quote source circuit opened for %.0fs after %d failures",
                source, self.circuit_cooldown, failures,
            )

    # ---- 错误分类 ---------------------------------------------------------
    def _is_transient(self, exc: BaseException) -> bool:
        if isinstance(exc, httpx.TimeoutException):
            return True
        # connect reset / 网络瞬断 -> 可重试
        if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.NetworkError, httpx.RemoteProtocolError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in _RETRYABLE_STATUS
        return False

    def _backoff_sleep(self, attempt: int) -> None:
        delay = min(self.backoff_cap, self.backoff_base * (2 ** attempt))
        jitter = self._rng() * delay  # full jitter
        if jitter > 0:
            self._sleep(jitter)

    # ---- 带重试的拉取 -----------------------------------------------------
    def _fetch_with_retry(self, url: str, source: str, headers: dict | None) -> str | None:
        """执行真实 HTTP, 对瞬态错误有界重试。成功返回文本, 否则 None。"""
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http_getter(url, headers=headers or {}, timeout=self.timeout, trust_env=False)
                resp.raise_for_status()
                text = resp.text
                if not text or not text.strip():
                    # schema 失败: 空响应, 不重试 (避免对空 body 反复打上游)
                    logger.warning("%s quote empty response (schema), not retrying", source)
                    return None
                return text
            except Exception as exc:  # noqa: BLE001
                if attempt < self.max_retries and self._is_transient(exc):
                    logger.debug("%s quote transient error (attempt %d), retrying: %s", source, attempt + 1, exc)
                    self._backoff_sleep(attempt)
                    continue
                logger.warning("%s quote GET failed (attempt %d): %s", source, attempt + 1, exc)
                return None
        return None

    def _guarded_fetch(self, url: str, source: str, headers: dict | None) -> str | None:
        """熔断 -> 限流 -> 重试拉取, 成功写缓存, 失败计熔断。"""
        if not self._source_available(source):
            return None
        self._enforce_rate_limit(source)
        text = self._fetch_with_retry(url, source, headers)
        if text is None:
            self._record_failure(source)
            return None
        self._record_success(source)
        self._cache_put(url, text)
        return text

    # ---- single-flight ----------------------------------------------------
    def _register_inflight(self, url: str) -> dict | None:
        """登记 inflight。返回 None 表示当前线程成为 leader; 返回 holder 表示需等待。"""
        with self._inflight_lock:
            existing = self._inflight.get(url)
            if existing is not None:
                return existing
            self._inflight[url] = {"event": threading.Event(), "result": None}
            return None

    def _complete_inflight(self, url: str, result: str | None) -> None:
        with self._inflight_lock:
            holder = self._inflight.pop(url, None)
        if holder is not None:
            holder["result"] = result
            holder["event"].set()

    # ---- 公共入口 ---------------------------------------------------------
    def _http_get(self, url: str, source: str, headers: dict | None = None) -> str | None:
        """受控拉取入口: allowlist -> 缓存 -> single-flight -> 熔断 -> 限流 -> 重试。"""
        # 1. Host allowlist — 任何场景不可绕过
        if not self._is_allowed_host(url):
            return None
        # 2. 短 TTL 缓存命中
        cached = self._cache_get(url)
        if cached is not None:
            return cached
        # 3. 同 URL single-flight: 并发调用去重为一次真实拉取
        holder = self._register_inflight(url)
        if holder is None:
            result = self._guarded_fetch(url, source, headers)
            self._complete_inflight(url, result)
            return result
        # follower: 复用 leader 结果 (含缓存, provenance 不变)
        holder["event"].wait()
        return holder["result"]

    def get_quotes(self, symbols: list[str], prefer: str = "tencent") -> list[dict]:
        codes = [_to_exch_code(s) for s in symbols if str(s).strip()]
        if not codes:
            return []

        source = "sina" if prefer == "sina" else "tencent"
        batch = SINA_BATCH if source == "sina" else TENCENT_BATCH
        rows: list[dict] = []

        for start in range(0, len(codes), batch):
            chunk = codes[start:start + batch]
            if source == "sina":
                text = self._http_get(
                    SINA_URL + ",".join(chunk),
                    "sina",
                    headers={"Referer": "https://finance.sina.com.cn"},
                )
                if text:
                    rows.extend(parse_sina(text, chunk))
                continue

            text = self._http_get(TENCENT_URL + ",".join(chunk), "tencent")
            if text:
                rows.extend(parse_tencent(text))
        return rows

    def get_depth(self, symbols: list[str]) -> dict:
        codes = [_to_exch_code(s) for s in symbols if str(s).strip()]
        if not codes:
            return {}

        rows: dict = {}
        for start in range(0, len(codes), TENCENT_BATCH):
            chunk = codes[start:start + TENCENT_BATCH]
            text = self._http_get(TENCENT_URL + ",".join(chunk), "tencent")
            if text:
                rows.update(parse_tencent_depth(text))
        return rows
