"""回测 SSE 端点的 start/end 入参校验 — 非法日期返回 400 而不是 500。

signals.py 的 `/intraday/replay` 与 mining.py 的 `MiningRunRequest` 都把
`date.fromisoformat` 包在校验里, 非法日期给出 400/422; backtest 的三个 SSE
端点直接裸调 `date.fromisoformat`, 同样的入参会抛未捕获 ValueError。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.backtest import router
from app.config import settings

# (路径, 该端点的必填参数)
STREAMS = [
    ("/api/backtest/strategy/stream", {"strategy_id": "ma_cross"}),
    # 旧 /optimize/stream 单窗样本内路径已于 P4 移除, 由稳健优化接替
    ("/api/backtest/optimize-robust/stream", {"strategy_id": "ma_cross", "param_grid": '{"n": [5]}'}),
    ("/api/backtest/walkforward/stream", {"strategy_id": "ma_cross", "param_grid": '{"n": [5]}'}),
]
STREAM_IDS = [path.rsplit("/", 2)[1] for path, _ in STREAMS]

BAD_DATES = ["not-a-date", "2026-13-01", "2026/09/04", ""]


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    # raise_server_exceptions=False: 未捕获异常表现为 500 响应, 与线上行为一致
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path,extra", STREAMS, ids=STREAM_IDS)
@pytest.mark.parametrize("bad", BAD_DATES)
def test_malformed_end_returns_400(client, path, extra, bad):
    if not bad:
        pytest.skip("空串走默认值分支, 不属于非法日期")
    resp = client.get(path, params={**extra, "end": bad})
    assert resp.status_code == 400, resp.text
    assert "日期" in resp.json()["detail"]


@pytest.mark.parametrize("path,extra", STREAMS, ids=STREAM_IDS)
@pytest.mark.parametrize("bad", BAD_DATES)
def test_malformed_start_returns_400(client, path, extra, bad):
    if not bad:
        pytest.skip("空串走默认值分支, 不属于非法日期")
    resp = client.get(path, params={**extra, "start": bad, "end": "2026-09-04"})
    assert resp.status_code == 400, resp.text
    assert "日期" in resp.json()["detail"]


@pytest.mark.parametrize("path,extra", STREAMS, ids=STREAM_IDS)
def test_valid_dates_still_accepted(client, monkeypatch, path, extra):
    """合法日期必须照旧进入事件流, 证明这不是把入口收窄成"什么都不收"。

    打开服务端范围保护并给一个超阈值的窗口, 事件流会立刻以 error 事件收尾,
    既不触发真实回测, 又能证明日期解析已经放行。
    """
    monkeypatch.setattr(settings, "backtest_range_guard", True)
    params = {**extra, "start": "2020-01-01", "end": "2026-09-04"}
    if "walkforward" in path:
        # walkforward 的 guard 作用于单折窗口, 不是总区间
        params.update({"train_days": 400, "test_days": 400})
    resp = client.get(path, params=params)
    assert resp.status_code == 200, resp.text
    assert "event: error" in resp.text
