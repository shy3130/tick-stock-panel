"""因子 API (validate / trial) 契约测试 (P2)。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.factors import router


@pytest.fixture()
def cleanup_registry():
    """测试注册的自定义因子在用例后注销, 不污染全局注册表 (快照测试依赖 77 基线)。"""
    created: set[str] = set()
    yield created
    from app.factors.registry import unregister_factor

    for fid in created:
        unregister_factor(fid)


class _FakeEngine:
    """合成面板: 3 只股票日收益固定 1%/2%/3%, 任何按价格排序的因子 IC 恒为 1。"""

    def __init__(self, n_days: int = 40) -> None:
        rows = []
        end = date.today()
        for index in range(n_days):
            day = end - timedelta(days=n_days - 1 - index)
            for symbol_id, daily_return in (("A", 0.01), ("B", 0.02), ("C", 0.03)):
                # 正基数且增速同序: C 永远最高价且回报最高 → 按价格排序的因子 IC 恒为 1
                rows.append({
                    "symbol": symbol_id,
                    "date": day,
                    "close": (1.0 + daily_return) ** index * 10.0 * (ord(symbol_id) - ord("A") + 1),
                })
        self.panel = pl.DataFrame(rows).sort(["symbol", "date"])

    def load_panel(self, symbols, start, end, *, columns=None, asset_type="stock", **_kwargs):
        frame = self.panel
        if columns is not None:
            for column in columns:
                if column not in frame.columns:
                    frame = frame.with_columns(pl.lit(None).cast(pl.Float64).alias(column))
            frame = frame.select(columns)
        return frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))


def _client(with_engine: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if with_engine:
        app.state.backtest_engine = _FakeEngine()
    return TestClient(app)


def test_validate_ok_formula() -> None:
    response = _client().post("/api/factors/validate", json={"formula": "rank(-ts_sum(change_pct, 5))"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["errors"] == []
    assert payload["dependencies"] == ["change_pct"]
    assert payload["warmup_bars"] == 6
    assert payload["cross_sectional"] is True


def test_validate_future_function_rejected() -> None:
    response = _client().post("/api/factors/validate", json={"formula": "rank(ts_delta(close, -5))"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "E005"
    assert "position" in payload["errors"][0]


def test_trial_golden_ic() -> None:
    response = _client(with_engine=True).post(
        "/api/factors/trial", json={"formula": "close", "days": 30},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["n_dates"] == 30  # 40 日面板, 首日无前收 → 39 个可算截面, 取最近 30
    assert payload["ic_mean"] == pytest.approx(1.0, abs=1e-9)
    assert payload["ic_win_rate"] == pytest.approx(1.0, abs=1e-9)
    # 恒定 IC 序列 std=0, IR 无定义 → None (除零保护)
    assert payload["ic_std"] in (None, 0.0)
    assert payload["ir"] is None
    assert len(payload["ic_series"]) == 30


def test_trial_compile_failure_400() -> None:
    response = _client(with_engine=True).post(
        "/api/factors/trial", json={"formula": "nope_col + 1", "days": 30},
    )
    assert response.status_code == 400
    body = response.json()["detail"]
    assert body["errors"][0]["code"] == "E001"


def test_trial_computes_virtual_factor_via_shared_path() -> None:
    # 引用虚拟因子 ma20_bias: 试算端点复用 _compute_missing_factors 补算路径
    # (compute_indicators 算 ma20 + materialize 物化 bias), 3 列粗面板即可出结果
    response = _client(with_engine=True).post(
        "/api/factors/trial", json={"formula": "ma20_bias", "days": 20},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["n_dates"] == 20
    assert payload["ic_mean"] is not None


@pytest.mark.parametrize(
    ("formula", "sign", "win_rate"),
    [("if_else(close > 40, close, 0)", 1.0, 1.0), ("if_else(close > 40, -close, 0)", -1.0, 0.0)],
)
def test_trial_excludes_undefined_ic_of_constant_cross_section(formula, sign, win_rate) -> None:
    """截面上公式值全相同的日子 Rank IC 无定义 (pl.corr 返回 NaN 而非 null)。

    离散公式常见 (如 ETF 池里的 if_else(change_pct > 0.05, 1, 0), 当天没有标的满足
    时全为 0)。因子检验 (FactorBacktestService) 与挖掘都按 is_finite 剔除这类日子;
    试算只滤 null, NaN 混进统计后 IC 均值/标准差/IR 全部变成 NaN (响应里为 null),
    胜率还把 NaN 当成正 IC 计入 (polars 中 NaN > 0 为真)。

    合成面板: 前 10 个截面三只股票全为 0 (IC 无定义), 之后 26 个截面 |IC| = √3/2
    (两只并列), 最后 3 个截面 |IC| = 1; 取最近 30 个截面时恰好混入 1 个无定义截面。
    """
    response = _client(with_engine=True).post(
        "/api/factors/trial", json={"formula": formula, "days": 30},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ic_mean"] == pytest.approx(sign * (26 * (3 ** 0.5 / 2) + 3) / 29, abs=1e-4)
    assert payload["ic_win_rate"] == pytest.approx(win_rate)
    assert payload["n_dates"] == 29
    assert [point["ic"] for point in payload["ic_series"] if point["ic"] is None] == []
    assert len(payload["ic_series"]) == 29


def test_group_and_status_update_after_registry_load(tmp_path, cleanup_registry) -> None:
    """改分组/状态在因子已注册 (启动加载后) 的真实路径下可用。

    回归保护: 同版本直接 register 会被注册表拒绝 ("版本未提升"),
    端点必须先注销再按新元数据注册。
    """
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors.registry import get_factor

    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)

    created = client.post("/api/factors/custom", json={
        "id": "uf_group_test", "label": "分组测试", "formula": "rank(-ts_sum(change_pct, 5))",
    })
    assert created.status_code == 200
    factor_id = created.json()["id"]
    cleanup_registry.add(factor_id)

    from app.factors import store

    store.load_into_registry(Path(tmp_path))  # 模拟重启后的注册状态
    registered = get_factor(factor_id)
    assert registered is not None and registered.group == "自定义"

    renamed = client.post(f"/api/factors/custom/{factor_id}/group", json={"group": "我的动量组"})
    assert renamed.status_code == 200
    assert renamed.json()["group"] == "我的动量组"
    refreshed = get_factor(factor_id)
    assert refreshed is not None and refreshed.group == "我的动量组"  # 注册表同步
    on_disk = next(d for d in store.load_all(Path(tmp_path)) if d["id"] == factor_id)
    assert on_disk["group"] == "我的动量组"  # 磁盘持久化

    activated = client.post(f"/api/factors/custom/{factor_id}/status", json={"status": "active"})
    assert activated.status_code == 200  # 修复前: 400 "版本未提升"
    assert get_factor(factor_id).stability == "stable"

    bad = client.post(f"/api/factors/custom/{factor_id}/group", json={"group": "   "})
    assert bad.status_code == 400  # 空白分组名 fail-closed


def test_update_custom_factor_bumps_version(tmp_path, cleanup_registry) -> None:
    """编辑已有自定义因子: 版本提升注册 + 公式变化回 draft + 试算门禁。"""
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors import store
    from app.factors.registry import get_factor

    cleanup_registry.add("uf_edit_test")
    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)
    data_dir = Path(tmp_path)

    created = client.post("/api/factors/custom", json={
        "id": "uf_edit_test", "label": "编辑测试", "formula": "rank(-ts_sum(change_pct, 5))",
    })
    assert created.status_code == 200
    assert created.json()["version"] == 1

    # 激活后再编辑: 公式变化 → 新版本 + 回 draft (生命周期语义)
    activated = client.post("/api/factors/custom/uf_edit_test/status", json={"status": "active"})
    assert activated.status_code == 200

    updated = client.post("/api/factors/custom/uf_edit_test/update", json={
        "label": "编辑测试v2", "group": "新分组", "formula": "rank(-ts_sum(change_pct, 10))",
        "description": "窗口从 5 改 10", "direction": "low",
    })
    assert updated.status_code == 200
    body = updated.json()
    assert body["version"] == 2 and body["status"] == "draft"

    spec = get_factor("uf_edit_test")
    assert spec is not None
    assert spec.version == 2 and spec.group == "新分组" and spec.label == "编辑测试v2"
    assert "change_pct" in spec.dependencies
    on_disk = next(d for d in store.load_all(data_dir) if d["id"] == "uf_edit_test")
    assert on_disk["version"] == 2 and on_disk["status"] == "draft"

    # 仅改元数据 (公式不变): 版本仍提升, 状态保留 (不回 draft)
    client.post("/api/factors/custom/uf_edit_test/status", json={"status": "active"})
    meta = client.post("/api/factors/custom/uf_edit_test/update", json={
        "label": "仅改名字", "group": "新分组", "formula": "rank(-ts_sum(change_pct, 10))",
    })
    assert meta.status_code == 200
    assert meta.json() == {"ok": True, "id": "uf_edit_test", "version": 3, "status": "active"}

    missing = client.post("/api/factors/custom/uf_nope/update", json={
        "label": "x", "formula": "close",
    })
    assert missing.status_code == 404


def test_trial_response_includes_newey_west_t() -> None:
    response = _client(with_engine=True).post(
        "/api/factors/trial", json={"formula": "close", "days": 30},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["n_dates"] == 30
    assert "t_newey_west" in payload  # 恒定 IC 序列下可为 None, 但字段必须存在


def test_delete_custom_and_composite_factor(tmp_path, cleanup_registry) -> None:
    """删除契约: 复合因子可直接删; 成员被复合引用时 409 列引用方, force 才放行。"""
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors import store
    from app.factors.registry import get_factor

    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()  # 创建门禁需试算
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)
    data_dir = Path(tmp_path)

    client.post("/api/factors/custom", json={
        "id": "uf_del_member", "label": "被引用成员", "formula": "rank(-ts_sum(change_pct, 5))",
    })
    cleanup_registry.add("uf_del_member")
    created = client.post("/api/factors/composite", json={
        "id": "cf_del_test", "label": "删除测试组合", "members": {"uf_del_member": 1.0, "momentum_10d": -0.5},
    })
    assert created.status_code == 200
    cleanup_registry.add("cf_del_test")

    # 404: 不存在的因子
    assert client.delete("/api/factors/custom/cf_nope").status_code == 404

    # 409: 成员被复合因子引用 → fail-closed, 返回引用方列表
    blocked = client.delete("/api/factors/custom/uf_del_member")
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert "cf_del_test" in str(detail["references"])
    assert get_factor("uf_del_member") is not None  # 引用未解除, 因子仍在

    # 复合因子本身可直接删除 (无人引用它)
    removed = client.delete("/api/factors/custom/cf_del_test")
    assert removed.status_code == 200
    assert removed.json() == {"ok": True, "id": "cf_del_test", "removed_references": []}
    assert get_factor("cf_del_test") is None
    assert all(d["id"] != "cf_del_test" for d in store.load_all(data_dir))  # 磁盘已删

    # 引用解除后成员可正常删除
    freed = client.delete("/api/factors/custom/uf_del_member")
    assert freed.status_code == 200
    assert get_factor("uf_del_member") is None


def test_delete_with_strategy_reference_requires_force(tmp_path, cleanup_registry) -> None:
    """策略文件引用同样拦截: 409 列出 strategies/*.json, force=true 放行。"""
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from app.factors.registry import get_factor

    app = FastAPI()
    app.include_router(router)
    app.state.backtest_engine = _FakeEngine()  # 创建门禁需试算
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    client = TestClient(app)
    data_dir = Path(tmp_path)

    client.post("/api/factors/custom", json={
        "id": "uf_strat_ref", "label": "策略引用", "formula": "rank(-ts_sum(change_pct, 5))",
    })
    cleanup_registry.add("uf_strat_ref")
    strategies_dir = data_dir / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "my_strategy.json").write_text(
        json.dumps({"name": "my_strategy", "factors": {"uf_strat_ref": 1.0}}), encoding="utf-8",
    )

    blocked = client.delete("/api/factors/custom/uf_strat_ref")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["references"] == ["strategies/my_strategy.json"]

    forced = client.delete("/api/factors/custom/uf_strat_ref?force=true")
    assert forced.status_code == 200
    assert forced.json()["removed_references"] == ["strategies/my_strategy.json"]
    assert get_factor("uf_strat_ref") is None


def test_list_factors_exposes_lifecycle_status(tmp_path, cleanup_registry):
    """列表接口须下发完整生命周期 status (draft/active/watch/retired)。

    stability 只是 status 的二元投影 (active→stable, 其余→experimental),
    管理界面的状态列/筛选靠完整 status; 内置因子无盘上定义, status 为 null。
    """
    import json as _json
    from types import SimpleNamespace

    from app.factors import store as factor_store

    definition = {
        "id": "uf_status_probe", "kind": "custom", "label": "状态探针",
        "group": "自定义", "formula": "rank(-ts_sum(change_pct, 5))",
        "description": "", "direction": "none", "version": 1, "status": "watch",
        "created_at": factor_store._now(), "updated_at": factor_store._now(),
    }
    factor_store.register_definition(definition)
    cleanup_registry.add("uf_status_probe")
    factor_dir = tmp_path / "user_data" / "custom_factors"
    factor_dir.mkdir(parents=True)
    (factor_dir / "uf_status_probe.json").write_text(
        _json.dumps(definition, ensure_ascii=False), encoding="utf-8",
    )

    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    client = TestClient(app)

    payload = client.get("/api/factors").json()
    probe = next(item for item in payload["factors"] if item["id"] == "uf_status_probe")
    assert probe["status"] == "watch"
    assert probe["stability"] == "experimental"  # 二元投影保留, 向后兼容

    builtin = next(item for item in payload["factors"] if item["kind"] == "base")
    assert builtin["status"] is None
