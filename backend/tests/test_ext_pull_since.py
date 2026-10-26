"""增量续传参数 (since_param): 盘中多盘表免每次全量重拉。

机制: 配置 pull.since_param + time_field 后, 每次拉取前读本地目标日分区
time_field 列最大值, 以 ?{since_param}={值} 附到请求 — 源只返回其后数据,
缺口感知天然成立 (本地缺多少源补多少)。本地无数据/列缺失/文件损坏时不带
参数 = 整日全量, 回退方向永远安全。

契约:
  - PullConfig 归一: 未配置 time_field 时 since_param 失效 (None)
  - 首拉 (无分区) 不带参数; 有分区带 max(time_field); 分区删除后又回全量
  - 分页拉取: since 作为过滤器与页码参数一起每页携带
  - 历史回补 (fetch_rows_for_date 不传 data_dir) 恒不带参数
  - 两级拉取 (fanout) 无整表游标语义, 不注入
  - API PullConfigReq.since_param 校验参数名合法性
mock httpx.MockTransport, 不发真实请求。
"""
from __future__ import annotations

from datetime import date

import httpx
import polars as pl
import pytest

from app.services import ext_pull
from app.services.ext_data import ExtConfig, ExtField, PullConfig

TARGET = date(2026, 10, 26)


def _config(**pull_over) -> ExtConfig:
    kwargs = dict(
        url="https://example.test/flow",
        response_path="data",
        time_field="ts",
        since_param="since",
    )
    kwargs.update(pull_over)
    return ExtConfig(
        id="flow",
        label="资金流",
        mode="timeseries",
        fields=[ExtField("symbol", "string"), ExtField("val", "float"), ExtField("ts", "string")],
        pull=PullConfig(**kwargs),
    )


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[str]:
    """MockTransport 记录每次请求 URL; 返回 URL 清单。"""
    urls: list[str] = []

    def _record(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return handler(request)

    real = httpx.AsyncClient

    def _factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(_record), **kwargs)

    monkeypatch.setattr(ext_pull.httpx, "AsyncClient", _factory)
    return urls


# ---------------------------------------------------------------- PullConfig 归一
def test_since_param_requires_time_field() -> None:
    p = PullConfig(time_field="ts", since_param="since")
    assert p.since_param == "since"
    assert PullConfig(since_param="since").since_param is None  # 无 time_field → 失效
    assert PullConfig(time_field="ts").since_param is None

    d = p.to_dict()
    assert d["since_param"] == "since"
    assert PullConfig.from_dict(d).since_param == "since"  # 往返不丢


def test_since_cursor_fallbacks(tmp_path) -> None:
    cfg = _config()
    # 无分区 → None (首拉/新交易日/删分区修复 → 全量)
    assert ext_pull.since_cursor(tmp_path, cfg, TARGET) is None

    part = ext_pull._day_partition(tmp_path, "flow", TARGET)
    part.parent.mkdir(parents=True, exist_ok=True)
    # 分区缺 ts 列 (schema 漂移) → None
    pl.DataFrame({"symbol": ["000001.SZ"]}).write_parquet(part)
    assert ext_pull.since_cursor(tmp_path, cfg, TARGET) is None

    # fanout 两级拉取无整表游标语义 → None
    fanout_cfg = _config(fanout={"url": "https://example.test/members"})
    assert ext_pull.since_cursor(tmp_path, fanout_cfg, TARGET) is None


# ---------------------------------------------------------------- E2E: 首拉全量 → 增量
def _rows(*slots: str) -> dict:
    return {"data": [
        {"symbol": "000001.SZ", "val": 10.0, "ts": s} for s in slots
    ]}


async def test_first_pull_full_then_incremental(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """首拉无分区不带 since (全量); 落库后二次拉取带 since=本地最大 ts;
    增量行并入同日分区, 按 [symbol, ts] 去重。"""
    urls = _install_transport(monkeypatch, lambda req: httpx.Response(
        200, json=_rows("1030") if "since=" in str(req.url) else _rows("0930", "1000"),
    ))

    n1, d1 = await ext_pull.fetch_and_ingest(_config(), tmp_path, TARGET)
    assert n1 == 2 and d1 == TARGET.isoformat()
    assert urls[0].endswith("/flow") and "since" not in urls[0]  # 首拉全量

    n2, _ = await ext_pull.fetch_and_ingest(_config(), tmp_path, TARGET)
    assert "since=1000" in urls[1]  # 游标 = 本地最大 ts
    assert n2 == 3  # 返回合并后分区总行数: 2 存量 + 1 增量

    part = ext_pull._day_partition(tmp_path, "flow", TARGET)
    df = pl.read_parquet(part)
    assert df.sort("ts")["ts"].to_list() == ["0930", "1000", "1030"]
    assert df.height == 3  # 不同盘并存, 同盘去重


async def test_partition_deleted_resumes_full(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """删分区修复: 游标随分区消失, 自动退回整日全量 (自愈路径)。"""
    mode = {"full": False}
    urls = _install_transport(monkeypatch, lambda req: httpx.Response(
        200, json=_rows("1000", "1030") if mode["full"] else _rows("0930"),
    ))
    cfg = _config()
    await ext_pull.fetch_and_ingest(cfg, tmp_path, TARGET)

    part = ext_pull._day_partition(tmp_path, "flow", TARGET)
    part.unlink()
    mode["full"] = True  # 换挡: 删分区后的下一次拉取走全量响应
    n, _ = await ext_pull.fetch_and_ingest(cfg, tmp_path, TARGET)
    assert n == 2
    assert "since" not in urls[1]  # 分区没了 → 不带游标 → 整日全量
    assert pl.read_parquet(part).height == 2  # 全量重建, 不残留旧盘


# ---------------------------------------------------------------- 与其他机制共存
async def test_since_with_date_param_and_pagination(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """since 与 date_param / 分页参数共存: 每页都带 since + 页码。"""
    cfg = _config(
        url="https://example.test/day",
        date_param="day",
        page_param="page",
        page_size_param="pageSize",
        page_size=1,
    )
    seen: list[dict | None] = []

    async def fake(pull, config_id, day=None, extra_params=None):
        seen.append(dict(extra_params) if extra_params else None)
        page = int((extra_params or {}).get("page", 1))
        return _rows("0930") if page == 1 else {"data": []}

    monkeypatch.setattr(ext_pull, "_request_json", fake)

    part = ext_pull._day_partition(tmp_path, "flow", TARGET)
    part.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["000001.SZ", "000002.SZ"], "ts": ["0900", "0900"]}).write_parquet(part)

    n, _ = await ext_pull.fetch_and_ingest(cfg, tmp_path, TARGET)
    assert n == 1
    assert seen[0] == {"since": "0900", "page": "1", "pageSize": "1"}  # 每页携带


async def test_backfill_path_never_sends_cursor(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """历史回补不传 data_dir → 恒全量 (回补只拉缺失分区, 游标无意义)。"""
    part = ext_pull._day_partition(tmp_path, "flow", TARGET)
    part.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["000001.SZ"], "ts": ["0900"]}).write_parquet(part)

    seen: list[dict | None] = []

    async def fake(pull, config_id, day=None, extra_params=None):
        seen.append(dict(extra_params) if extra_params else None)
        return _rows("0930")

    monkeypatch.setattr(ext_pull, "_request_json", fake)
    rows = await ext_pull.fetch_rows_for_date(_config(), TARGET)  # 公开入口 (回补同款)
    assert len(rows) == 1 and seen == [None]  # 本地有数据也不带游标


# ---------------------------------------------------------------- API 参数校验
def test_api_request_model_validates_since_param() -> None:
    from app.api.ext_data import PullConfigReq

    assert PullConfigReq(url="https://x", since_param="since").since_param == "since"
    with pytest.raises(ValueError):
        PullConfigReq(url="https://x", since_param="bad-name")  # 参数名只允许字母数字下划线
