"""通用两级拉取 (ext_fanout) 单测 — 目录 → 逐条目明细 → 反转为「成员 → 类别」。

全程 httpx.MockTransport, 不出网。覆盖: 信封解包报错、反转聚合与目录顺序、
单条目失败容忍(重试后跳过)、全部条目失败报错、必填项校验、
以及端到端落盘 (config.json + parquet, 走 fetch_and_ingest 通用路径)。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import polars as pl
import pytest

from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    ExtField,
    PullConfig,
    rows_to_parquet,
)
from app.services.ext_fanout import clean_label, fetch_fanout_rows

_CATALOG = "/api/a-share-index/catalog/ths-index-list"
_CONSTITUENTS = "/api/a-share-index/constituents/ths-stock-list"
_AUTH_HEADER = {"X-api-key": "test-key"}


def _ok(items: list[dict]) -> httpx.Response:
    return httpx.Response(
        200, json={"code": 0, "message": "success", "data": {"item": items}}
    )


def _mock_transport(
    catalog_by_tag: dict[str, list[dict]],
    constituents_by_code: dict[str, list[dict]],
    fail_codes: tuple[str, ...] = (),
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _CATALOG:
            tag = request.url.params.get("tag", "cn_concept")
            return _ok(catalog_by_tag.get(tag, []))
        if request.url.path == _CONSTITUENTS:
            code = request.url.params.get("thscode", "")
            if code in fail_codes:
                raise httpx.ConnectError(f"injected failure: {code}")
            return _ok(constituents_by_code.get(code, []))
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


_CATALOG_CN_CONCEPT = [
    {"thscode": "B1.TI", "name": "白酒概念"},
    {"thscode": "B2.TI", "name": "新能源汽车"},
]
_CONSTITUENTS_BY_CODE = {
    "B1.TI": [
        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
        {"thscode": "000858.SZ", "ticker": "000858", "name": "五粮液"},
    ],
    "B2.TI": [
        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
        {"thscode": "300750.SZ", "ticker": "300750", "name": "宁德时代"},
    ],
}


def _concept_config(url_tag: str = "cn_concept", **fanout_overrides) -> ExtConfig:
    """测试用概念表配置: 结构与出厂预设同构, 便于断言 schema。"""
    fanout = {
        "url": f"https://fuyao.aicubes.cn{_CONSTITUENTS}",
        "param": "thscode",
        "id_field": "thscode",
        "label_field": "name",
        "items_path": "data.item",
        "member_id_field": "thscode",
        "member_label_field": "name",
        "symbol_col": "股票代码",
        "name_col": "股票简称",
        "value_col": "所属概念",
        "separator": ";",
        "concurrency": 8,
        "retries": 1,
    }
    fanout.update(fanout_overrides)
    return ExtConfig(
        id="ext_gn_ths",
        label="扩展概念",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("code", "string", "代码"),
            ExtField("股票代码", "string", "股票代码"),
            ExtField("股票简称", "string", "股票简称"),
            ExtField("所属概念", "string", "所属概念"),
        ],
        symbol_map={"type": "mapped", "col": "股票代码"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=PullConfig(
            url=f"https://fuyao.aicubes.cn{_CATALOG}?tag={url_tag}",
            response_path="data.item",
            auth={"type": "header", "header": "X-api-key"},
            fanout=fanout,
        ),
    )


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch):
    """把 ext_pull 的出站请求替换为 MockTransport (鉴权与标识头口径保持不变)。

    同时注入测试 Key: _apply_auth 在缺凭据时 fail-closed 报错, 这是生产要的
    行为, 测 fanout 逻辑时需补上凭据。
    """
    import app.services.ext_pull as ext_pull
    import app.services.ext_data as ext_data

    monkeypatch.setattr(
        ext_data, "get_ext_api_key", lambda config_id: "test-key"
    )

    def install(transport: httpx.MockTransport):
        real_request = ext_pull._request_json

        async def patched(pull, config_id, day=None, extra_params=None, *, url_override=None):
            # 复用真实实现的 URL/鉴权拼装, 只把 transport 换成 Mock
            from app.services.ext_pull import _apply_auth, _append_query_params, _with_date_param
            url = url_override or (
                _with_date_param(pull.url, pull.date_param, day, pull.date_format) if day else pull.url
            )
            if extra_params:
                url = _append_query_params(url, extra_params)
            headers = {"User-Agent": "test", "X-TSP-Client": "tick-stock-panel"}
            url = _apply_auth(config_id, pull.auth, url, headers)
            async with httpx.AsyncClient(timeout=30, transport=transport) as client:
                resp = await client.request(pull.method.upper(), url, headers=headers)
                resp.raise_for_status()
                return resp.json()

        monkeypatch.setattr(ext_pull, "_request_json", patched)
        return real_request

    return install


# ---- 值清洗 ----


def test_clean_label_drops_missing_value_placeholders() -> None:
    for placeholder in (None, float("nan"), "nan", "None", " null ", ""):
        assert clean_label(placeholder) == ""
    assert clean_label(" 银行 ") == "银行"


# ---- 必填项校验 ----


def test_fanout_missing_required_keys_raises() -> None:
    config = _concept_config()
    config.pull.fanout.pop("member_id_field")
    with pytest.raises(ValueError, match="缺少必填项"):
        asyncio.run(fetch_fanout_rows(config))


# ---- 信封解包 ----


def test_fanout_envelope_error_raises(mock_http) -> None:
    mock_http(
        httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": 2001, "message": "invalid key"})
        )
    )
    with pytest.raises(ValueError, match="code=2001"):
        asyncio.run(fetch_fanout_rows(_concept_config()))


# ---- 反转聚合 ----


def test_fanout_inverts_by_catalog_order(mock_http) -> None:
    mock_http(_mock_transport({"cn_concept": _CATALOG_CN_CONCEPT}, _CONSTITUENTS_BY_CODE))

    rows = asyncio.run(fetch_fanout_rows(_concept_config()))
    by_symbol = {r["股票代码"]: r for r in rows}
    assert len(by_symbol) == 3
    # 类别名跟随目录顺序拼接 (B1 在前 B2 在后), 与请求完成顺序无关
    assert by_symbol["600519.SH"]["所属概念"] == "白酒概念;新能源汽车"
    assert by_symbol["600519.SH"]["股票简称"] == "贵州茅台"
    assert by_symbol["000858.SZ"]["所属概念"] == "白酒概念"
    assert by_symbol["300750.SZ"]["所属概念"] == "新能源汽车"


def test_fanout_tolerates_entry_failure(mock_http) -> None:
    """单条目重试后仍失败 → 跳过该条目, 其余正常返回。"""
    mock_http(
        _mock_transport(
            {"cn_concept": _CATALOG_CN_CONCEPT},
            _CONSTITUENTS_BY_CODE,
            fail_codes=("B2.TI",),
        )
    )
    rows = asyncio.run(fetch_fanout_rows(_concept_config()))
    by_symbol = {r["股票代码"]: r for r in rows}
    assert set(by_symbol) == {"600519.SH", "000858.SZ"}  # B2 成员被跳过
    assert by_symbol["600519.SH"]["所属概念"] == "白酒概念"


def test_fanout_all_entries_failed_raises(mock_http) -> None:
    mock_http(
        _mock_transport(
            {"cn_concept": _CATALOG_CN_CONCEPT},
            _CONSTITUENTS_BY_CODE,
            fail_codes=("B1.TI", "B2.TI"),
        )
    )
    with pytest.raises(ValueError, match="全部条目第二级拉取失败"):
        asyncio.run(fetch_fanout_rows(_concept_config()))


def test_fanout_empty_catalog_raises(mock_http) -> None:
    mock_http(_mock_transport({"cn_concept": []}, {}))
    with pytest.raises(ValueError, match="第一级返回 0 个可用条目"):
        asyncio.run(fetch_fanout_rows(_concept_config()))


def test_fanout_dedupes_repeated_labels(mock_http) -> None:
    """同一标的在同一类别下重复出现 → 只记一次。"""
    mock_http(
        _mock_transport(
            {"cn_concept": [{"thscode": "B1.TI", "name": "白酒概念"}]},
            {
                "B1.TI": [
                    {"thscode": "600519.SH", "name": "贵州茅台"},
                    {"thscode": "600519.SH", "name": "贵州茅台"},
                ]
            },
        )
    )
    rows = asyncio.run(fetch_fanout_rows(_concept_config()))
    assert rows[0]["所属概念"] == "白酒概念"


# ---- 端到端落盘 (通用写入链) ----


def test_fanout_rows_persist_via_generic_writer(
    tmp_path: Path, mock_http
) -> None:
    mock_http(_mock_transport({"cn_concept": _CATALOG_CN_CONCEPT}, _CONSTITUENTS_BY_CODE))
    config = _concept_config()

    rows = asyncio.run(fetch_fanout_rows(config))
    n = rows_to_parquet(rows, config, tmp_path)
    assert n == 3

    out = tmp_path / "ext_data" / "ext_gn_ths" / "part.parquet"
    assert out.exists(), "快照 parquet 未落盘"

    df = pl.read_parquet(out)
    row = df.filter(pl.col("symbol") == "600519.SH").to_dicts()[0]
    assert row["所属概念"] == "白酒概念;新能源汽车"
    assert row["股票简称"] == "贵州茅台"
    assert row["code"] == "600519"


def test_fanout_industry_separator(mock_http) -> None:
    """行业配方用 "-" 拼接层级, 概念用 ";": 同一引擎, 差别只在配方。"""
    mock_http(
        _mock_transport(
            {"industry": [{"thscode": "H1.TI", "name": "金融"}]},
            {"H1.TI": [{"thscode": "600519.SH", "name": "贵州茅台"}]},
        )
    )
    config = _concept_config(
        url_tag="industry", value_col="所属同花顺行业", separator="-"
    )
    rows = asyncio.run(fetch_fanout_rows(config))
    assert rows[0]["所属同花顺行业"] == "金融"


def test_pull_config_roundtrip_preserves_fanout(tmp_path: Path) -> None:
    """fanout 随 config.json 持久化与读回, 不丢字段。"""
    config = _concept_config()
    store = ExtConfigStore(tmp_path)
    store.upsert(config)

    loaded = ExtConfigStore(tmp_path).get("ext_gn_ths")
    assert loaded is not None and loaded.pull is not None
    assert loaded.pull.fanout == config.pull.fanout


# ---- 429 限流: 配额窗口退避 + 失败补拉轮 ----

@pytest.fixture(autouse=True)
def _fast_fanout_sleep(monkeypatch: pytest.MonkeyPatch):
    """补拉轮/限流退避不真实等待 (需要断言退避时长的用例再覆盖为记录型)。"""
    import app.services.ext_fanout as fanout

    async def _nosleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(fanout, "_sleep", _nosleep)


def _flaky_transport(fail_times: dict[str, int]) -> tuple[httpx.MockTransport, dict[str, int]]:
    """对指定 thscode 的前 N 次成分请求返回 429, 之后恢复成功。"""
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _CATALOG:
            return _ok(_CATALOG_CN_CONCEPT)
        if request.url.path == _CONSTITUENTS:
            code = request.url.params.get("thscode", "")
            n = calls.get(code, 0)
            calls[code] = n + 1
            if n < fail_times.get(code, 0):
                return httpx.Response(429, json={"code": 429, "message": "Too Many Requests"})
            return _ok(_CONSTITUENTS_BY_CODE.get(code, []))
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler), calls


def test_fanout_429_replenish_round_recovers_entries(mock_http) -> None:
    """主轮内 429 重试仍失败 → 补拉轮跨过配额窗口后补齐, 最终数据完整。"""
    transport, calls = _flaky_transport({"B2.TI": 2})  # 主轮 2 次尝试均 429
    mock_http(transport)

    rows = asyncio.run(fetch_fanout_rows(_concept_config()))

    labels = {r["所属概念"] for r in rows}
    assert any("新能源汽车" in v for v in labels)      # B2 补拉成功
    assert any("白酒概念" in v for v in labels)        # B1 不受影响
    assert calls["B2.TI"] >= 3                          # 主轮 2 次 + 补拉至少 1 次


def test_fanout_429_persistent_gives_up_after_replenish_rounds(mock_http) -> None:
    """持续限流: 用尽 (retries+1)×(1+补拉轮) 次尝试后跳过该条目, 其余正常。"""
    transport, calls = _flaky_transport({"B2.TI": 10_000})
    mock_http(transport)

    rows = asyncio.run(fetch_fanout_rows(_concept_config()))

    labels = {r["所属概念"] for r in rows}
    assert any("白酒概念" in v for v in labels)                    # B1 正常
    assert not any("新能源汽车" in v for v in labels)               # B2 放弃
    assert calls["B2.TI"] == 2 * 4  # retries=1 → 2 次/轮 × (主轮+3 补轮)


def test_fanout_429_uses_quota_window_backoff(mock_http, monkeypatch: pytest.MonkeyPatch) -> None:
    """429 的单条目退避 ≥ 配额窗口 (30s), 而非普通失败的 0.5s。"""
    import app.services.ext_fanout as fanout

    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(fanout, "_sleep", record_sleep)
    transport, _ = _flaky_transport({"B2.TI": 1})  # 第 1 次 429, 重试成功
    mock_http(transport)

    rows = asyncio.run(fetch_fanout_rows(_concept_config()))

    assert any("新能源汽车" in r["所属概念"] for r in rows)
    assert sleeps and max(sleeps) >= 30.0
