"""自定义源 financial 数据集回归。

一个 financial 配置覆盖全部五表(metrics/income/balance_sheet/cash_flow/
shares): 请求时注入 table 参数由上游分发; shares 表额外注入 latest。
字段只要求映射出 symbol, 指标列按 canonical 口径宽表透传, 无
period_end 的帧由同步层合并链路自然过滤。
"""
from __future__ import annotations

import polars as pl

from app.data_providers.custom.config import config_from_dict
from app.data_providers.custom.provider import GenericHTTPProvider


def _provider(**overrides) -> GenericHTTPProvider:
    cfg = {
        "url": "http://example.test/financial",
        "response_path": "data",
        "field_map": {
            "symbol": "symbol", "period_end": "period_end",
            "announce_date": "announce_date",
            "revenue": "revenue", "net_income": "net_income",
        },
        **overrides,
    }
    return GenericHTTPProvider(config_from_dict({
        "name": "eltdx", "datasets": {"financial": cfg},
    }))


def test_financial_request_injects_table_parameter(monkeypatch):
    provider = _provider()
    captured: dict = {}

    def fake_request(cfg, **kwargs):
        captured.update(kwargs)
        return [{"symbol": "600000.SH", "period_end": "2026-06-30",
                 "announce_date": "2026-08-30",
                 "revenue": 1.0, "net_income": 0.1}]

    monkeypatch.setattr(provider, "_request_rows", fake_request)
    df = provider.get_financials("income", ["600000.SH"])

    assert captured["symbols"] == ["600000.SH"]
    assert captured["override_params"]["table"] == "income"
    assert captured["override_body"]["table"] == "income"
    assert df.columns == ["symbol", "period_end", "announce_date", "revenue", "net_income"]
    assert df["symbol"][0] == "600000.SH"


def test_financial_shares_request_injects_latest_flag(monkeypatch):
    provider = _provider()
    captured: dict = {}
    monkeypatch.setattr(
        provider, "_request_rows",
        lambda cfg, **kwargs: (captured.update(kwargs), [])[1],
    )

    provider.get_financials("shares", ["600000.SH"], latest_only=True)
    assert captured["override_params"]["table"] == "shares"
    assert captured["override_params"]["latest"] is True

    provider.get_financials("shares", ["600000.SH"], latest_only=False)
    assert captured["override_params"]["latest"] is False

    provider.get_financials("metrics", ["600000.SH"])
    # 非 shares 表不注入 latest
    assert "latest" not in captured["override_params"]


def test_financial_validate_skips_date_parameter_conflicts():
    # financial 逐批只注入代码参数: 起止时间不参与请求,
    # 其与 symbols_param 重名不应被判 duplicate
    provider = _provider(symbols_param="range", start_param="range")
    assert provider.validate() == []

    assert _provider().validate() == []


def test_financial_test_dataset_probes_metrics_table(monkeypatch):
    provider = _provider()
    captured: dict = {}
    monkeypatch.setattr(
        provider, "_request_rows",
        lambda cfg, **kwargs: (captured.update(kwargs), [
            {"symbol": "600000.SH", "period_end": "2026-06-30"},
        ])[1],
    )

    out = provider.test_dataset("financial", ["600000.SH"])
    assert out["dataset"] == "financial"
    assert out["rows"] == 1
    # 测试与生产同口径: 注入 table=metrics
    assert captured["override_params"]["table"] == "metrics"


def test_financial_sparse_column_survives_schema_inference(monkeypatch):
    """稀疏列回归: batch=100 时前 100 行全 null 的列不得被采样推断成 Null 列。

    旧行为: pl.DataFrame(rows) 默认 100 行采样 → Null dtype → 第 101 行数值
    append ComputeError → 整表拉空 (fuyao 插件同因踩坑)。
    """
    provider = _provider()
    rows = [
        {"symbol": f"00000{i}.SZ", "period_end": "2026-06-30",
         "announce_date": "2026-08-30", "revenue": None}
        for i in range(100)
    ]
    rows.append({"symbol": "600519.SH", "period_end": "2026-06-30",
                 "announce_date": "2026-08-30", "revenue": 5.0e10})
    monkeypatch.setattr(provider, "_request_rows", lambda cfg, **kwargs: rows)

    df = provider.get_financials("metrics", ["600519.SH"])
    assert df["revenue"].dtype == pl.Float64
    assert df["revenue"].drop_nulls().to_list() == [5.0e10]


def test_duplicate_mapped_targets_are_rejected():
    """两个外部字段映射同一内部列: rename 阶段 DuplicateError → 整表拉空,
    validate 必须在保存时拦截 (前端映射行允许选重)。"""
    provider = GenericHTTPProvider(config_from_dict({
        "name": "eltdx",
        "datasets": {"financial": {
            "url": "http://example.test/financial",
            "field_map": {"symbol": "symbol", "eps": "eps_basic",
                          "eps_dil": "eps_basic"},  # eps_dil 与 eps 同目标
        }},
    }))
    errors = provider.validate()
    assert "financial: duplicate mapped target fields: eps_basic" in errors
