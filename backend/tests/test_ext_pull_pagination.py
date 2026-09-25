"""分页拉取协议 (page_param/page_size_param): 按页循环、空页/短页判停、
max_pages 安全上限、非分页配置行为不变。mock _request_json, 不发真实请求。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services import ext_pull
from app.services.ext_data import ExtConfig, ExtField, PullConfig


def _cfg(pull: PullConfig) -> ExtConfig:
    return ExtConfig(
        id="hot",
        label="人气",
        mode="timeseries",
        fields=[ExtField("symbol", "string"), ExtField("heat", "float")],
        pull=pull,
    )


def _install_pages(monkeypatch: pytest.MonkeyPatch, pages: dict[int, list[dict]]) -> list[dict]:
    """按 extra_params["page"] 返回预置页; 记录每次调用收到的 extra_params。"""
    seen: list[dict | None] = []

    async def fake(pull, config_id, day=None, extra_params=None):
        seen.append(dict(extra_params) if extra_params else None)
        key = (extra_params or {}).get("page")
        page = int(key) if key is not None else 1
        return {"data": pages.get(page, [])}

    monkeypatch.setattr(ext_pull, "_request_json", fake)
    return seen


TARGET = date(2026, 9, 20)


async def test_pagination_collects_pages_and_stops_on_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    # page_size=2: 第 2 页只有 1 行 (短页) → 停, 不再请求第 3 页
    pull = PullConfig(
        url="https://api.example.com/hot", response_path="data",
        page_param="page", page_size_param="pageSize", page_size=2, max_pages=10,
    )
    seen = _install_pages(monkeypatch, {
        1: [{"symbol": "000001.SZ", "heat": 1}, {"symbol": "600519.SH", "heat": 2}],
        2: [{"symbol": "300750.SZ", "heat": 3}],
        3: [{"symbol": "不oshould到达", "heat": 9}],
    })
    rows = await ext_pull.fetch_rows_for_date(_cfg(pull), TARGET)
    assert [r["symbol"] for r in rows] == ["000001.SZ", "600519.SH", "300750.SZ"]
    assert len(seen) == 2
    assert seen[0] == {"page": "1", "pageSize": "2"}
    assert seen[1] == {"page": "2", "pageSize": "2"}


async def test_pagination_stops_on_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    # 未知 page_size: 只能靠空页判停
    pull = PullConfig(url="https://api.example.com/hot", response_path="data", page_param="page")
    seen = _install_pages(monkeypatch, {
        1: [{"symbol": "000001.SZ", "heat": 1}],
        2: [],
    })
    rows = await ext_pull.fetch_rows_for_date(_cfg(pull), TARGET)
    assert len(rows) == 1 and len(seen) == 2
    assert seen[0] == {"page": "1"}  # 未配置 page_size_param → 不发送 size 参数


async def test_pagination_respects_max_pages_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    pull = PullConfig(url="https://api.example.com/hot", response_path="data", page_param="page", max_pages=3)
    seen = _install_pages(monkeypatch, {
        1: [{"symbol": "a", "heat": 1}], 2: [{"symbol": "b", "heat": 1}], 3: [{"symbol": "c", "heat": 1}],
    })
    # 每页都满 (无 page_size 信息) → 达上限 3 页停
    rows = await ext_pull.fetch_rows_for_date(_cfg(pull), TARGET)
    assert len(rows) == 3 and len(seen) == 3


async def test_pagination_page_start_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    pull = PullConfig(url="https://api.example.com/hot", response_path="data", page_param="page", page_start=0)
    seen = _install_pages(monkeypatch, {0: [{"symbol": "000001.SZ", "heat": 1}]})
    # page 0 有 1 行, page 1 空 → 停
    rows = await ext_pull.fetch_rows_for_date(_cfg(pull), TARGET)
    assert len(rows) == 1 and seen[0] == {"page": "0"}


async def test_no_page_param_keeps_single_request(monkeypatch: pytest.MonkeyPatch) -> None:
    pull = PullConfig(url="https://api.example.com/hot", response_path="data")
    seen = _install_pages(monkeypatch, {1: [{"symbol": "000001.SZ", "heat": 1}]})
    rows = await ext_pull.fetch_rows_for_date(_cfg(pull), TARGET)
    assert len(rows) == 1 and seen == [None]  # 单次请求不带 extra_params


async def test_post_with_page_param_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # 分页仅支持 GET; POST 配了 page_param 也按单次请求 (不注页参数)
    pull = PullConfig(url="https://api.example.com/hot", method="POST", body="{}", response_path="data", page_param="page")
    seen = _install_pages(monkeypatch, {1: [{"symbol": "000001.SZ", "heat": 1}]})
    rows = await ext_pull.fetch_rows_for_date(_cfg(pull), TARGET)
    assert len(rows) == 1 and seen == [None]
