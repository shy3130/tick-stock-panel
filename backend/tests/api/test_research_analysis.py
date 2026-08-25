"""研究分析 API 测试 — 单标的日收益的风险/绩效/ADF/GARCH。

覆盖：正常、样本不足（含空 canonical 数据）、读取异常（503）、非法代码（422）、
日期顺序错误（422）、超 5 年（422）、非有限数值、GARCH JSON-safe。

不读取真实 data/；全部用内存 Polars frame + mock repo。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import research_analysis
from app.backtest.portfolio import returns_from_prices



# ── fixtures ─────────────────────────────────────────────────────────────


class _FakeRepo:
    """Mock repo with get_daily_asset returning a Polars frame."""

    def __init__(self, frame: pl.DataFrame | None = None, *, raise_exc: Exception | None = None):
        self._frame = frame
        self._raise = raise_exc

    def get_daily_asset(
        self,
        asset_type,
        symbol,
        start,
        end,
        columns=None,
        *,
        raise_on_error=False,
    ):
        if self._raise is not None:
            if raise_on_error:
                raise self._raise
            return pl.DataFrame()
        return self._frame if self._frame is not None else pl.DataFrame()


def _price_frame(symbol: str, dates: list[date], closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol] * len(dates),
        "date": dates,
        "close": closes,
    }).with_columns(pl.col("date").cast(pl.Date))


def _client(repo: _FakeRepo) -> TestClient:
    app = FastAPI()
    app.include_router(research_analysis.router)
    app.state.repo = repo
    return TestClient(app)


def _dates(n: int, *, end: date | None = None) -> list[date]:
    """n consecutive trading-ish dates ending on ``end`` (default today)."""
    end = end or date.today()
    return [end - timedelta(days=n - 1 - i) for i in range(n)]


def _trending_closes(n: int, *, start: float = 10.0, drift: float = 0.001) -> list[float]:
    """n closes with small random-ish drift (deterministic, no external RNG)."""
    closes = []
    base = start
    for i in range(n):
        base *= 1.0 + drift * (1 if i % 3 else -1)
        closes.append(round(base, 4))
    return closes


# ── 正常路径 ──────────────────────────────────────────────────────────────


def test_normal_analysis_returns_full_envelope():
    n = 60
    sym = "600519.SH"
    frame = _price_frame(sym, _dates(n), _trending_closes(n))
    client = _client(_FakeRepo(frame))

    resp = client.get(f"/api/research/analysis/symbol/{sym}")
    assert resp.status_code == 200
    body = resp.json()

    # envelope 固定键
    expected_keys = {
        "available", "source", "symbol", "start", "end", "data_as_of",
        "observations", "result", "warnings", "reason",
    }
    assert set(body.keys()) == expected_keys
    assert body["available"] is True
    assert body["source"] == "canonical-enriched"
    assert body["symbol"] == sym
    assert body["reason"] is None
    assert body["observations"] == n - 1  # returns lose 1 point

    # result 三段
    result = body["result"]
    assert set(result.keys()) == {"risk", "performance", "statistics"}
    assert result["risk"]["status"] == "ok"
    assert result["performance"]["status"] == "ok"
    assert set(result["statistics"].keys()) == {"adf", "garch"}

    # GARCH conditional_variance → list (not ndarray)
    assert isinstance(result["statistics"]["garch"]["conditional_variance"], list)

    # data_as_of 是序列最后一个日期
    last_date = _dates(n)[-1]
    assert body["data_as_of"] == last_date.isoformat()


def test_default_window_is_one_year():
    sym = "600000.SH"
    n = 40
    frame = _price_frame(sym, _dates(n), _trending_closes(n))
    client = _client(_FakeRepo(frame))

    resp = client.get(f"/api/research/analysis/symbol/{sym}")
    assert resp.status_code == 200
    body = resp.json()
    today = date.today()
    default_start = today - timedelta(days=365)
    assert body["start"] == default_start.isoformat()
    assert body["end"] == today.isoformat()


# ── 样本不足 ──────────────────────────────────────────────────────────────


def test_insufficient_returns_returns_200_with_insufficient_status():
    """Only 1 price point → 0 returns → 200 + insufficient_data everywhere."""
    sym = "000001.SZ"
    frame = _price_frame(sym, [date.today()], [10.0])
    client = _client(_FakeRepo(frame))

    resp = client.get(f"/api/research/analysis/symbol/{sym}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["observations"] == 0
    assert body["result"]["risk"]["status"] == "insufficient_data"
    assert body["result"]["performance"]["status"] == "insufficient_data"
    assert body["result"]["statistics"]["adf"]["status"] == "insufficient_data"
    assert body["result"]["statistics"]["garch"]["status"] == "insufficient_data"
    assert len(body["warnings"]) > 0


def test_few_returns_risk_insufficient_but_adf_may_succeed():
    """5 price points → 4 returns: risk needs 30 (insufficient), adf needs 3 (ok)."""
    sym = "000002.SZ"
    n = 5
    frame = _price_frame(sym, _dates(n), _trending_closes(n))
    client = _client(_FakeRepo(frame))

    resp = client.get(f"/api/research/analysis/symbol/{sym}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["observations"] == 4
    assert body["result"]["risk"]["status"] == "insufficient_data"
    # ADF needs >= 3 observations → 4 is enough
    assert body["result"]["statistics"]["adf"]["status"] == "ok"


# ── 空数据 / 读取异常 ────────────────────────────────────────────────────


def test_no_data_returns_200_with_insufficient_status():
    """A valid empty canonical query is insufficient data, not a source outage."""
    sym = "600519.SH"
    client = _client(_FakeRepo(pl.DataFrame()))

    resp = client.get(f"/api/research/analysis/symbol/{sym}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["source"] == "canonical-enriched"
    assert body["data_as_of"] is None
    assert body["observations"] == 0
    assert body["reason"] is None
    assert body["result"]["risk"]["status"] == "insufficient_data"
    assert body["result"]["performance"]["status"] == "insufficient_data"
    assert body["result"]["statistics"]["adf"]["status"] == "insufficient_data"
    assert body["result"]["statistics"]["garch"]["status"] == "insufficient_data"
    assert "no canonical enriched data" in body["warnings"][0]


def test_repo_exception_returns_503():
    """Repository failures remain distinguishable from valid empty results."""
    sym = "600519.SH"
    client = _client(_FakeRepo(raise_exc=RuntimeError("catalog unavailable")))

    resp = client.get(f"/api/research/analysis/symbol/{sym}")
    assert resp.status_code == 503
    body = resp.json()
    assert body["available"] is False
    assert body["result"] is None
    assert body["source"] is None
    assert "canonical data source unavailable" in body["reason"]
    assert "catalog unavailable" in body["reason"]

# ── 非法代码 → 422 ────────────────────────────────────────────────────────


def test_invalid_symbol_returns_422():
    client = _client(_FakeRepo(pl.DataFrame()))

    for bad in [
        "600519",
        "600519.HK",
        "600519.INDEX",
        "abc123.SH",
        "60051.SH",
        "6005199.SH",
        "１２３４５６.SH",
    ]:
        resp = client.get(f"/api/research/analysis/symbol/{bad}")
        assert resp.status_code == 422, f"{bad} should be 422"


# ── 日期顺序错误 → 422 ────────────────────────────────────────────────────


def test_start_after_end_returns_422():
    sym = "600519.SH"
    client = _client(_FakeRepo(pl.DataFrame()))
    resp = client.get(
        f"/api/research/analysis/symbol/{sym}",
        params={"start": "2026-01-01", "end": "2025-01-01"},
    )
    assert resp.status_code == 422


# ── 超 5 年 → 422 ──────────────────────────────────────────────────────────


def test_range_over_5_years_returns_422():
    sym = "600519.SH"
    client = _client(_FakeRepo(pl.DataFrame()))
    start = (date.today() - timedelta(days=365 * 5 + 10)).isoformat()
    resp = client.get(
        f"/api/research/analysis/symbol/{sym}",
        params={"start": start},
    )
    assert resp.status_code == 422


# ── 非有限数值（NaN/Inf）→ 统一剔除 ─────────────────────────────────────


def test_non_finite_closes_are_excluded_from_observations():
    """Envelope and downstream risk use the same finite daily-return sample."""
    sym = "600519.SH"
    n = 60
    closes = _trending_closes(n)
    closes[10] = float("nan")
    closes[20] = float("inf")
    closes[30] = float("-inf")
    expected_returns = returns_from_prices(
        np.asarray(closes, dtype=float).reshape(-1, 1)
    )[:, 0]
    expected_observations = int(np.isfinite(expected_returns).sum())
    client = _client(_FakeRepo(_price_frame(sym, _dates(n), closes)))

    resp = client.get(f"/api/research/analysis/symbol/{sym}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["observations"] == expected_observations
    assert body["result"]["risk"]["observations"] == expected_observations
    assert any("dropped" in warning for warning in body["warnings"])

    import json

    re_serialized = json.dumps(body, allow_nan=False)
    assert "NaN" not in re_serialized
    assert "Infinity" not in re_serialized


# ── BJ exchange symbol accepted ──────────────────────────────────────────


def test_bj_symbol_accepted():
    sym = "430047.BJ"
    n = 40
    frame = _price_frame(sym, _dates(n), _trending_closes(n))
    client = _client(_FakeRepo(frame))

    resp = client.get(f"/api/research/analysis/symbol/{sym}")
    assert resp.status_code == 200
    assert resp.json()["symbol"] == sym


# ── explicit start/end honored ───────────────────────────────────────────


def test_explicit_start_end_honored():
    sym = "600519.SH"
    n = 60
    frame = _price_frame(sym, _dates(n), _trending_closes(n))
    client = _client(_FakeRepo(frame))

    explicit_start = (date.today() - timedelta(days=30)).isoformat()
    explicit_end = (date.today() - timedelta(days=10)).isoformat()
    resp = client.get(
        f"/api/research/analysis/symbol/{sym}",
        params={"start": explicit_start, "end": explicit_end},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["start"] == explicit_start
    assert body["end"] == explicit_end
