"""扩展数据历史回补测试 — date_param 按日拉取 / 日期一致性防污染 / 幂等回补。

背景: 人气排行等时序扩展表历史只能从开启拉取之日起累积; backfill_history
按本地交易日逐日回补。金融契约重点: 接口忽略日期参数返回当日数据时
必须拒写历史分区 (实测确有此类接口), 否则整个时序口径错乱。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

import httpx
import polars as pl
import pytest

from app.services import ext_pull
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig
from app.services.ext_pull import (
    _assert_rows_date,
    _with_date_param,
    backfill_history,
    fetch_rows_for_date,
)


def _cfg(mode: str = "timeseries", date_param: str | None = "date") -> ExtConfig:
    return ExtConfig(
        id="hot", label="人气", mode=mode,
        fields=[
            ExtField("symbol", "string"), ExtField("rank", "int"),
            ExtField("date", "string"), ExtField("heat", "float"),
        ],
        pull=PullConfig(url="https://example.test/rank", date_param=date_param),
    )


def _row(sym: str, day: str) -> dict:
    return {"symbol": sym, "rank": 1, "date": day, "heat": 9.9}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """按请求 URL 中的日期参数返回预置数据; 记录全部请求 URL。

    responses/calls 用函数属性 (class-annotation) 而非实例属性:
    ext_pull 以 ``httpx.AsyncClient(**kw)`` 工厂方式构造, fixture 借类属性
    注入预置数据, RUF012 mutable-default 面由此声明为 ClassVar 语义。
    """

    responses: ClassVar[dict[str, list]] = {}
    calls: ClassVar[list[str]] = []
    header_calls: ClassVar[list[dict]] = []  # 每次请求实际发送的 headers
    errors: ClassVar[dict[str, Exception]] = {}
    fail_times: ClassVar[dict[str, int]] = {}  # url -> 还需失败的次数
    error_sequence: ClassVar[dict[str, list[Exception]]] = {}  # url -> 按序抛出后耗尽

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method: str, url: str, **kwargs):
        _FakeClient.calls.append(url)
        _FakeClient.header_calls.append(dict(kwargs.get("headers") or {}))
        seq = _FakeClient.error_sequence.get(url)
        if seq:
            raise seq.pop(0)
        if url in _FakeClient.errors and _FakeClient.fail_times.get(url, 1) > 0:
            _FakeClient.fail_times[url] = _FakeClient.fail_times.get(url, 1) - 1
            raise _FakeClient.errors[url]
        return _FakeResp(_FakeClient.responses.get(url, []))


@pytest.fixture()
def fake_http(monkeypatch):
    _FakeClient.responses = {}
    _FakeClient.calls = []
    _FakeClient.header_calls = []
    _FakeClient.errors = {}
    _FakeClient.fail_times = {}
    _FakeClient.error_sequence = {}
    monkeypatch.setattr(ext_pull.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


# ── 纯函数 ────────────────────────────────────────────────

def test_outbound_headers_default_and_override():
    """出站标识头: 默认带 tsp UA + X-TSP-Client; 用户同名头优先 (大小写不敏感)。"""
    from app.services.ext_pull import outbound_headers

    h = outbound_headers()
    assert h["User-Agent"].startswith("tsp/")
    assert h["X-TSP-Client"] == "tick-stock-panel"

    h2 = outbound_headers({"user-agent": "my-ua", "X-Custom": "1"})
    assert h2["user-agent"] == "my-ua"          # 小写同名覆盖默认 UA
    assert "User-Agent" not in h2                # 不重复发送
    assert h2["X-TSP-Client"] == "tick-stock-panel"  # 未覆盖的标识头保留
    assert h2["X-Custom"] == "1"


async def test_fetch_rows_carries_tsp_identity(fake_http):
    fake_http.responses["https://example.test/rank?date=2026-01-05"] = [_row("A", "2026-01-05")]
    await fetch_rows_for_date(_cfg(), date(2026, 1, 5))
    headers = fake_http.header_calls[-1]
    assert headers["User-Agent"].startswith("tsp/")
    assert headers["X-TSP-Client"] == "tick-stock-panel"


async def test_fetch_rows_user_headers_take_precedence(fake_http):
    cfg = ExtConfig(
        id="hot", label="人气", mode="timeseries",
        fields=_cfg().fields,
        pull=PullConfig(
            url="https://example.test/rank",
            headers={"User-Agent": "custom-ua"},
            date_param="date",
        ),
    )
    fake_http.responses["https://example.test/rank?date=2026-01-05"] = [_row("A", "2026-01-05")]
    await fetch_rows_for_date(cfg, date(2026, 1, 5))
    headers = fake_http.header_calls[-1]
    assert headers["User-Agent"] == "custom-ua"
    assert headers["X-TSP-Client"] == "tick-stock-panel"


def test_with_date_param_url_building():
    d = date(2026, 1, 5)
    assert _with_date_param("https://x/api", "date", d) == "https://x/api?date=2026-01-05"
    assert _with_date_param("https://x/api?a=1", "date", d) == "https://x/api?a=1&date=2026-01-05"
    assert _with_date_param("https://x/api", None, d) == "https://x/api"


def test_with_date_param_formats():
    """date_format 分支: compact=YYYYMMDD; ts_s/ts_ms = 该交易日北京时间 00:00:00 时间戳。"""
    d = date(2026, 1, 5)
    assert _with_date_param("https://x/api", "date", d, "compact") == "https://x/api?date=20260105"
    ts_s = int(datetime(2026, 1, 5, tzinfo=timezone(timedelta(hours=8))).timestamp())
    assert _with_date_param("https://x/api", "ts", d, "ts_s") == f"https://x/api?ts={ts_s}"
    assert _with_date_param("https://x/api", "ts", d, "ts_ms") == f"https://x/api?ts={ts_s * 1000}"
    # 未知格式回退 iso
    assert _with_date_param("https://x/api", "date", d, "bogus") == "https://x/api?date=2026-01-05"


def test_pull_config_date_format_roundtrip_and_normalize():
    """date_format 配置往返保留; 手改 config.json 写入非法值时归一为 iso。"""
    cfg = PullConfig(url="https://x/api", date_param="ts", date_format="ts_ms")
    assert cfg.date_format == "ts_ms"
    restored = PullConfig.from_dict(cfg.to_dict())
    assert restored.date_format == "ts_ms"
    assert PullConfig(date_param="date", date_format="bogus").date_format == "iso"
    assert PullConfig.from_dict({"date_param": "date"}).date_format == "iso"  # 旧配置缺字段


def test_assert_rows_date_contract():
    d = date(2026, 1, 5)
    _assert_rows_date([_row("A", "2026-01-05")], d)                     # 一致
    _assert_rows_date([_row("A", "2026-01-05 00:00:00")], d)            # 带时间前缀
    _assert_rows_date([{"symbol": "A"}], d)                             # 无 date 字段: 不校验
    with pytest.raises(ValueError, match="不一致"):
        _assert_rows_date([_row("A", "2026-09-06")], d)                 # 接口忽略参数返回当日


def test_pull_config_date_param_roundtrip():
    p = PullConfig(url="u", date_param="date")
    assert p.to_dict()["date_param"] == "date"
    legacy = PullConfig.from_dict({"url": "u"})  # 旧 JSON 无该键
    assert legacy.date_param is None


# ── fetch_rows_for_date ───────────────────────────────────

async def test_fetch_rows_builds_dated_url(fake_http):
    fake_http.responses["https://example.test/rank?date=2026-01-05"] = [_row("A", "2026-01-05")]
    rows = await fetch_rows_for_date(_cfg(), date(2026, 1, 5))
    assert rows and rows[0]["symbol"] == "A"
    assert fake_http.calls == ["https://example.test/rank?date=2026-01-05"]


async def test_fetch_rows_rejects_mismatched_date(fake_http):
    # 接口忽略 ?date= 返回当日数据 → 必须拒收, 不给 backfill 写历史分区的机会
    fake_http.responses["https://example.test/rank?date=2026-01-05"] = [_row("A", "2026-09-06")]
    with pytest.raises(ValueError, match="不一致"):
        await fetch_rows_for_date(_cfg(), date(2026, 1, 5))


async def test_fetch_rows_empty_returns_empty_list(fake_http):
    assert await fetch_rows_for_date(_cfg(), date(2026, 1, 5)) == []


async def test_fetch_rows_requires_symbol_or_code(fake_http):
    fake_http.responses["https://example.test/rank?date=2026-01-05"] = [{"rank": 1}]
    with pytest.raises(ValueError, match="symbol/code"):
        await fetch_rows_for_date(_cfg(), date(2026, 1, 5))


# ── backfill_history ──────────────────────────────────────

def _seed_trading_days(data_dir: Path, *days: str) -> None:
    root = data_dir / "kline_daily"
    for d in days:
        (root / f"date={d}").mkdir(parents=True, exist_ok=True)


async def test_backfill_rejects_snapshot_and_missing_date_param(tmp_path):
    with pytest.raises(ValueError, match="timeseries"):
        await backfill_history(_cfg(mode="snapshot"), tmp_path, date(2026, 1, 5), date(2026, 1, 9))
    with pytest.raises(ValueError, match="date_param"):
        await backfill_history(_cfg(date_param=None), tmp_path, date(2026, 1, 5), date(2026, 1, 9))


async def test_backfill_rejects_bad_range_and_no_trading_days(tmp_path):
    with pytest.raises(ValueError, match="晚于"):
        await backfill_history(_cfg(), tmp_path, date(2026, 1, 9), date(2026, 1, 5))
    with pytest.raises(ValueError, match="上限"):
        await backfill_history(_cfg(), tmp_path, date(2025, 1, 1), date(2026, 9, 1))
    with pytest.raises(ValueError, match="交易日"):
        await backfill_history(_cfg(), tmp_path, date(2026, 1, 5), date(2026, 1, 9))


async def test_backfill_writes_partitions_and_is_idempotent(tmp_path, fake_http):
    _seed_trading_days(tmp_path, "2026-01-02", "2026-01-05", "2026-01-06")
    # 01-02: 已有分区 → 跳过; 01-05: 接口有数据 → 写入; 01-06: 接口空 → empty
    part_102 = tmp_path / "ext_data" / "hot" / "timeseries" / "date=2026-01-02" / "part.parquet"
    part_102.parent.mkdir(parents=True)
    part_102.write_bytes(b"x")
    fake_http.responses["https://example.test/rank?date=2026-01-05"] = [
        _row("600000.SH", "2026-01-05"), _row("000001.SZ", "2026-01-05"),
    ]

    result = await backfill_history(_cfg(), tmp_path, date(2026, 1, 1), date(2026, 1, 9))
    assert result["total_days"] == 3
    assert result["fetched"] == 1 and result["rows_written"] == 2
    assert result["skipped_existing"] == 1
    assert result["empty"] == 1 and result["failed"] == []
    written = tmp_path / "ext_data" / "hot" / "timeseries" / "date=2026-01-05" / "part.parquet"
    assert written.exists()

    # 幂等: 已写入的分区不再请求; empty 日 (无分区文件) 允许重试
    fake_http.calls.clear()
    again = await backfill_history(_cfg(), tmp_path, date(2026, 1, 1), date(2026, 1, 9))
    assert again["skipped_existing"] == 2 and again["fetched"] == 0 and again["empty"] == 1
    assert fake_http.calls == ["https://example.test/rank?date=2026-01-06"]


async def test_backfill_collects_failures_without_abort(tmp_path, fake_http):
    _seed_trading_days(tmp_path, "2026-01-05", "2026-01-06")
    # 01-05: 接口忽略参数返回当日 → 拒写 (failed); 01-06: 正常写入
    fake_http.responses["https://example.test/rank?date=2026-01-05"] = [_row("A", "2026-09-06")]
    fake_http.responses["https://example.test/rank?date=2026-01-06"] = [_row("A", "2026-01-06")]

    result = await backfill_history(_cfg(), tmp_path, date(2026, 1, 5), date(2026, 1, 6))
    assert result["fetched"] == 1
    assert [f["date"] for f in result["failed"]] == ["2026-01-05"]
    assert "不一致" in result["failed"][0]["reason"]
    assert not (tmp_path / "ext_data/hot/timeseries/date=2026-01-05").exists()


# ── 与扩展消费链路的衔接 ─────────────────────────────────

async def test_backfilled_partition_feeds_signal_frame(tmp_path, fake_http):
    """回补落盘的历史分区, ext_factors 按日对齐立即可用 (PIT)。"""
    from app.factors import ext_factors

    ExtConfigStore(tmp_path).upsert(_cfg())
    _seed_trading_days(tmp_path, "2026-01-05", "2026-01-06")
    fake_http.responses["https://example.test/rank?date=2026-01-05"] = [
        {"symbol": "600000.SH", "rank": 3, "date": "2026-01-05", "heat": 88.0},
    ]
    fake_http.responses["https://example.test/rank?date=2026-01-06"] = [
        {"symbol": "600000.SH", "rank": 1, "date": "2026-01-06", "heat": 99.0},
    ]
    await backfill_history(_cfg(), tmp_path, date(2026, 1, 5), date(2026, 1, 6))

    frame = pl.DataFrame(
        {"symbol": ["600000.SH", "600000.SH"], "date": ["2026-01-05", "2026-01-06"]},
        schema={"symbol": pl.Utf8, "date": pl.Utf8},
    )
    out = ext_factors.attach_ext_columns(frame, include_snapshot=False, data_dir=tmp_path)
    assert out["ext_hot_rank"].to_list() == [3.0, 1.0]      # rank int → Float64
    assert out["ext_hot_heat"].to_list() == [88.0, 99.0]    # 每日各自的值, 无串日


# ── 429 限流退避与中止 ─────────────────────


def _err_429(url: str) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", url)
    return httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req))


async def test_backfill_429_retries_once_and_succeeds(tmp_path, fake_http, monkeypatch):
    monkeypatch.setattr(ext_pull, "_BACKFILL_429_WAIT_S", 0)
    monkeypatch.setattr(ext_pull, "_BACKFILL_DAY_INTERVAL_S", 0)
    _seed_trading_days(tmp_path, "2026-01-05")
    url = "https://example.test/rank?date=2026-01-05"
    fake_http.errors[url] = _err_429(url)
    fake_http.fail_times[url] = 1  # 仅首请 429, 重试成功
    fake_http.responses[url] = [_row("A", "2026-01-05")]

    result = await backfill_history(_cfg(), tmp_path, date(2026, 1, 5), date(2026, 1, 5))
    assert result["fetched"] == 1 and result["failed"] == []
    assert fake_http.calls.count(url) == 2  # 首请 429 + 退避重试


async def test_backfill_aborts_after_consecutive_429(tmp_path, fake_http, monkeypatch):
    monkeypatch.setattr(ext_pull, "_BACKFILL_429_WAIT_S", 0)
    monkeypatch.setattr(ext_pull, "_BACKFILL_DAY_INTERVAL_S", 0)
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    _seed_trading_days(tmp_path, *days)
    for d in days:
        u = f"https://example.test/rank?date={d}"
        fake_http.errors[u] = _err_429(u)
        fake_http.fail_times[u] = 2  # 首请 + 重试均 429

    result = await backfill_history(_cfg(), tmp_path, date(2026, 1, 5), date(2026, 1, 9))
    assert result["fetched"] == 0
    # 连续计数达到 3 即中止: 前两日各 2 次请求, 第 3 日首个 429 立即中止
    assert len(fake_http.calls) == 5
    aborts = [f for f in result["failed"] if f["reason"].startswith("限流中止")]
    assert len(result["failed"]) == 5 and len(aborts) == 3  # 剩余未请求的日标记为可续补


# ── 404 无快照日 (tickflow-hub 契约) ──────────────────────


def _err_404(url: str) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", url)
    return httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))


async def test_backfill_404_counts_as_empty_not_failed(tmp_path, fake_http, monkeypatch):
    """hub /exports、/fuyao-rank 契约: 该日无快照返回 404 → empty 跳过, 不进失败清单。"""
    monkeypatch.setattr(ext_pull, "_BACKFILL_DAY_INTERVAL_S", 0)
    _seed_trading_days(tmp_path, "2026-01-05", "2026-01-06", "2026-01-07")
    u5, u6, u7 = (f"https://example.test/rank?date={d}" for d in ("2026-01-05", "2026-01-06", "2026-01-07"))
    fake_http.errors[u5] = _err_404(u5)
    fake_http.responses[u6] = [_row("600000.SH", "2026-01-06")]
    fake_http.errors[u7] = _err_404(u7)

    result = await backfill_history(_cfg(), tmp_path, date(2026, 1, 5), date(2026, 1, 7))
    assert result["fetched"] == 1 and result["empty"] == 2
    assert result["failed"] == []  # 404 不是失败
    assert result["rows_written"] == 1
    assert (tmp_path / "ext_data/hot/timeseries/date=2026-01-06/part.parquet").exists()


async def test_backfill_404_after_429_retry_counts_as_empty(tmp_path, fake_http, monkeypatch):
    """429 退避重试后返回 404: 同样视为该日无数据, 不进失败清单。"""
    monkeypatch.setattr(ext_pull, "_BACKFILL_429_WAIT_S", 0)
    monkeypatch.setattr(ext_pull, "_BACKFILL_DAY_INTERVAL_S", 0)
    _seed_trading_days(tmp_path, "2026-01-05", "2026-01-06")
    u5 = "https://example.test/rank?date=2026-01-05"
    u6 = "https://example.test/rank?date=2026-01-06"
    fake_http.error_sequence[u5] = [_err_429(u5), _err_404(u5)]  # 首请 429 → 重试 404
    fake_http.responses[u6] = [_row("A", "2026-01-06")]

    result = await backfill_history(_cfg(), tmp_path, date(2026, 1, 5), date(2026, 1, 6))
    assert result["fetched"] == 1 and result["empty"] == 1
    assert result["failed"] == []
    assert fake_http.calls.count(u5) == 2  # 确认重试确实发生
