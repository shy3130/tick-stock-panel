import threading
from unittest.mock import patch

import httpx

from app.data_providers.fquant.sina_tencent_client import (
    SINA_URL,
    TENCENT_URL,
    SinaTencentClient,
    parse_sina,
    parse_tencent,
    parse_tencent_depth,
    _to_exch_code,
)


TENCENT_FIXTURE = (
    # Real sample checked 2026-07-02:
    # v_sh600519="1~贵州茅台~600519~1203.00~1193.01~1193.01~50870...
    'v_sh600519="1~贵州茅台~600519~1193.01~1185.49~1180.10~42473~21000~21473~'
    + "~".join(["0"] * 24)
    + '~1196.80~1166.33~1193.01/42473/5033838080~42473~503383~'
    + "~".join(["0"] * 10)
    + '";'
)

SINA_FIXTURE = (
    # Real sample checked 2026-07-02:
    # var hq_str_sh600519="贵州茅台,1193.010,1193.010,1203.000...
    'var hq_str_sh600519="贵州茅台,1180.100,1185.490,1193.010,1196.800,1166.330,'
    '1192.900,1193.010,4247300,5033838080.000,'
    + ",".join(["0"] * 20)
    + ',2026-07-02,14:30:00,00";'
)


def test_parse_tencent():
    rows = parse_tencent(TENCENT_FIXTURE)
    row = rows[0]
    assert row["symbol"] == "600519.SH"
    assert row["last_price"] == 1193.01
    assert row["prev_close"] == 1185.49
    assert row["volume"] == 42473 * 100


def test_parse_tencent_depth_preserves_zero_queue():
    parts = ["0"] * 49
    parts[1] = "测试"
    parts[2] = "600000"
    parts[9] = "10.00"
    parts[10] = "123"
    parts[19] = "10.01"
    parts[20] = "0"
    text = 'v_sh600000="' + "~".join(parts) + '";'

    row = parse_tencent_depth(text)["600000.SH"]

    assert row["bid_prices"][0] == 10.0
    assert row["bid_volumes"][0] == 123
    assert row["ask_prices"][0] == 10.01
    assert row["ask_volumes"][0] == 0


def test_parse_sina():
    rows = parse_sina(SINA_FIXTURE, ["sh600519"])
    row = rows[0]
    assert row["symbol"] == "600519.SH"
    assert row["last_price"] == 1193.01
    assert row["volume"] == 4247300
    assert row["timestamp"] == "2026-07-02T14:30:00"


def test_batch_chunking():
    client = SinaTencentClient()
    with patch.object(client, "_http_get", return_value=None) as get:
        client.get_quotes([f"{600000 + i}.SH" for i in range(130)], prefer="tencent")
        assert get.call_count == 3


def test_partial_failure_keeps_success_rows():
    client = SinaTencentClient()
    with patch.object(client, "_http_get", side_effect=[TENCENT_FIXTURE, None]):
        rows = client.get_quotes(["600519.SH"] + [f"{600000 + i}.SH" for i in range(60)], prefer="tencent")
        assert any(row["symbol"] == "600519.SH" for row in rows)


def test_source_cooldown_after_three_failures():
    client = SinaTencentClient()
    client._record_failure("tencent")
    client._record_failure("tencent")
    assert client._source_available("tencent")

    client._record_failure("tencent")
    assert not client._source_available("tencent")

    client._record_success("tencent")
    assert client._source_available("tencent")


def test_depth_batch_chunking():
    client = SinaTencentClient()
    with patch.object(client, "_http_get", return_value=None) as get:
        client.get_depth([f"{600000 + i}.SH" for i in range(130)])
        assert get.call_count == 3


def test_hk_symbol_uses_hk_prefix_for_depth():
    assert _to_exch_code("00700.HK") == "hk00700"

    parts = ["0"] * 49
    parts[1] = "腾讯控股"
    parts[2] = "00700"
    parts[9] = "300.0"
    parts[10] = "100"
    parts[19] = "300.2"
    parts[20] = "200"
    text = 'v_hk00700="' + "~".join(parts) + '";'

    assert "00700.HK" in parse_tencent_depth(text)


# ---------------------------------------------------------------------------
# M19 受控 HTTP 可靠性测试
# ---------------------------------------------------------------------------


class _FakeResponse:
    """最小化的 httpx 响应替身, 支持 raise_for_status 与 status_code 分类。"""

    def __init__(self, text: str = "data", status: int = 200) -> None:
        self.text = text
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=httpx.Request("GET", "https://qt.gtimg.cn/"),
                response=httpx.Response(self.status_code),
            )


def _client(http_getter, **kw):
    """构造一个无副作用 (no real sleep/网络) 的客户端, 用于可靠性测试。"""
    return SinaTencentClient(
        min_interval=0.0,
        cache_ttl=0.0,
        sleeper=lambda _s: None,
        clock=lambda: 0.0,
        rng=lambda: 0.0,
        http_getter=http_getter,
        **kw,
    )


def test_host_allowlist_blocks_unknown_host():
    calls = []

    def getter(url, **kw):
        calls.append(url)
        return _FakeResponse("x")

    client = _client(getter)
    # allowlisted host 放行
    assert client._http_get(TENCENT_URL + "sh000001", "tencent") == "x"
    # 非 allowlist host 被拒, getter 不被调用
    assert client._http_get("https://evil.example.com/q=sh000001", "tencent") is None
    assert len(calls) == 1


def test_response_cache_avoids_duplicate_fetch_within_ttl():
    calls = []

    def getter(url, **kw):
        calls.append(url)
        return _FakeResponse("cached")

    times = [100.0]
    client = SinaTencentClient(
        min_interval=0.0, cache_ttl=5.0, sleeper=lambda _s: None,
        clock=lambda: times[0], rng=lambda: 0.0, http_getter=getter,
    )
    url = TENCENT_URL + "sh000001"
    assert client._http_get(url, "tencent") == "cached"
    assert client._http_get(url, "tencent") == "cached"  # 命中缓存
    assert len(calls) == 1
    times[0] += 6  # 过期
    assert client._http_get(url, "tencent") == "cached"
    assert len(calls) == 2


def test_single_flight_dedupes_concurrent_same_url():
    call_count = 0
    started = threading.Event()
    release = threading.Event()

    def getter(url, **kw):
        nonlocal call_count
        call_count += 1
        started.set()
        release.wait(2)
        return _FakeResponse("sf")

    client = SinaTencentClient(
        min_interval=0.0, cache_ttl=0.0, sleeper=lambda _s: None,
        rng=lambda: 0.0, http_getter=getter,
    )
    url = TENCENT_URL + "sh000001"
    results = []

    def worker():
        results.append(client._http_get(url, "tencent"))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    assert started.wait(2)
    t2.start()  # leader 已登记 inflight, follower 应等待而非重复拉取
    release.set()
    t1.join(2)
    t2.join(2)
    assert call_count == 1
    assert results == ["sf", "sf"]


def test_transient_network_error_retried_then_succeeds():
    attempts = []

    def getter(url, **kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("transient")
        return _FakeResponse("ok")

    slept = []
    client = SinaTencentClient(
        min_interval=0.0, cache_ttl=0.0, sleeper=slept.append,
        rng=lambda: 0.5, http_getter=getter, max_retries=2,
    )
    assert client._http_get(TENCENT_URL + "sh000001", "tencent") == "ok"
    assert len(attempts) == 2
    assert len(slept) == 1  # 退避 sleep 一次


def test_connect_reset_is_retryable():
    attempts = []

    def getter(url, **kw):
        attempts.append(1)
        if len(attempts) <= 2:
            raise httpx.ConnectError("[Errno 54] Connection reset by peer")
        return _FakeResponse("ok")

    client = _client(getter, max_retries=3)
    assert client._http_get(TENCENT_URL + "sh000001", "tencent") == "ok"
    assert len(attempts) == 3


def test_retryable_statuses_retried_until_exhausted():
    for status in (429, 502, 503, 504):
        calls = []

        def getter(url, **kw):
            calls.append(1)
            return _FakeResponse(status=status)

        client = _client(getter, max_retries=2)
        assert client._http_get(TENCENT_URL + "sh000001", "tencent") is None
        assert len(calls) == 3  # 初次 + 2 次重试


def test_non_retryable_statuses_not_retried():
    for status in (400, 401, 403, 404):
        calls = []

        def getter(url, **kw):
            calls.append(1)
            return _FakeResponse(status=status)

        client = _client(getter, max_retries=3)
        assert client._http_get(TENCENT_URL + "sh000001", "tencent") is None
        assert len(calls) == 1  # 非瞬态, 不重试


def test_empty_schema_response_not_retried():
    calls = []

    def getter(url, **kw):
        calls.append(1)
        return _FakeResponse(text="   ")

    client = _client(getter, max_retries=3)
    assert client._http_get(TENCENT_URL + "sh000001", "tencent") is None
    assert len(calls) == 1


def test_circuit_breaker_blocks_requests_and_logs_recovery(caplog):
    calls = []

    def getter(url, **kw):
        calls.append(1)
        raise httpx.ConnectError("down")

    client = _client(getter, max_retries=0, circuit_threshold=2)
    url = TENCENT_URL + "sh000001"
    # max_retries=0: 每次 _http_get 仅一次尝试, 计一次失败
    assert client._http_get(url, "tencent") is None
    assert client._http_get(url, "tencent") is None
    assert not client._source_available("tencent")  # 熔断打开

    calls_before = len(calls)
    assert client._http_get(url, "tencent") is None  # 熔断期不再发请求
    assert len(calls) == calls_before

    with caplog.at_level("INFO", logger="app.data_providers.fquant.sina_tencent_client"):
        client._record_success("tencent")
    assert client._source_available("tencent")
    assert any("recovered" in r.message for r in caplog.records)


def test_per_host_rate_limit_enforces_min_interval():
    times = [0.0]
    slept = []

    def getter(url, **kw):
        return _FakeResponse("d")

    client = SinaTencentClient(
        min_interval=0.5, cache_ttl=0.0, sleeper=slept.append,
        clock=lambda: times[0], rng=lambda: 0.0, http_getter=getter,
    )
    client._http_get(TENCENT_URL + "sh000001", "tencent")
    # 不同 URL 绕过缓存, 但同 host -> 触发限流 sleep
    client._http_get(TENCENT_URL + "sh600519", "tencent")
    assert any(s > 0 for s in slept)
