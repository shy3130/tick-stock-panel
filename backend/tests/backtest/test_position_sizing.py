"""仓位策略契约与注册表 — 校验/冒烟/包装器往返/隔离加载。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.backtest import position_policies as pp
from app.backtest.position_sizing import (
    PositionSizingResult,
    normalize_result,
    smoke_compute,
    synthetic_context,
)


def _result(weights, scale=1.0):
    return PositionSizingResult(weights=weights, budget_scale=scale)


# ── normalize_result ────────────────────────────────────────

def test_normalize_accepts_and_normalizes() -> None:
    weights, scale = normalize_result(_result([2.0, 1.0, 1.0]), 3)
    assert weights == pytest.approx([0.5, 0.25, 0.25])
    assert scale == 1.0


def test_normalize_clamps_scale_and_negative_weights() -> None:
    weights, scale = normalize_result(_result([3.0, -1.0, 2.0], scale=1.7), 3)
    assert weights == pytest.approx([0.6, 0.0, 0.4])
    assert scale == 1.0


@pytest.mark.parametrize(
    "weights,n,scale",
    [
        ([1.0, 1.0], 3, 1.0),  # 长度不符
        ([0.0, 0.0], 2, 1.0),  # 和为 0
        ([1.0, float("nan")], 2, 1.0),  # 非有限值
        ([1.0, 1.0], 2, 0.0),  # scale 非法
        ([1.0, 1.0], 2, -0.5),
    ],
)
def test_normalize_rejects_invalid(weights, n, scale) -> None:
    with pytest.raises(ValueError, match=r"权重|budget_scale|有限值|大于 0|长度"):
        normalize_result(_result(weights, scale), n)


def test_normalize_rejects_non_result_type() -> None:
    with pytest.raises(ValueError, match="PositionSizingResult"):
        normalize_result({"weights": [1.0]}, 1)  # type: ignore[arg-type]


# ── 冒烟 ──────────────────────────────────────────────────

def test_smoke_ok_for_equal_policy() -> None:
    def compute(ctx):
        n = len(ctx.selected)
        return _result([1.0 / n] * n)

    out = smoke_compute(compute)
    assert out["ok"], out["issues"]
    assert sum(out["weights"]) == pytest.approx(1.0, abs=1e-4)


def test_smoke_reports_runtime_error_in_chinese() -> None:
    def compute(ctx):
        raise RuntimeError("boom")

    out = smoke_compute(compute)
    assert not out["ok"]
    assert any("compute 执行失败" in i for i in out["issues"])


def test_smoke_catches_bad_output() -> None:
    def compute(ctx):
        return _result([0.0] * len(ctx.selected))

    out = smoke_compute(compute)
    assert not out["ok"]
    assert any("大于 0" in i for i in out["issues"])


def test_context_is_frozen_and_causal_shape() -> None:
    ctx = synthetic_context(4)
    with pytest.raises((AttributeError, TypeError)):
        ctx.selected = ()  # type: ignore[misc]
    assert ctx.api_version == 1
    assert all(s.score >= 0 for s in ctx.selected)
    assert ctx.portfolio.drawdown <= 0
    assert len(ctx.recent_returns) == len(ctx.selected)


# ── 包装器往返与注册表 ─────────────────────────────────────

def test_render_parse_roundtrip() -> None:
    meta = {
        "id": "up_rt_check",
        "label": "往返测试",
        "description": "desc",
        "params": [{"id": "w", "label": "窗口", "type": "int", "default": 10, "min": 1, "max": 60, "step": 1}],
    }
    code = pp.TEMPLATE_VOL_TARGET["code"]
    assert pp.render_policy_file(meta, code)
    meta2, code2 = pp.parse_policy_file(pp.render_policy_file(meta, code))
    assert meta2 == meta
    assert code2.strip() == code.strip()


def test_validate_rejects_dangerous_code(tmp_path: Path) -> None:
    bad = (
        "import os\n\n"
        'META = {"id": "up_bad", "label": "危险", "params": []}\n\n'
        "def compute(ctx):\n"
        "    os.system('ls')\n"
        "    return PositionSizingResult(weights=[1.0], budget_scale=1.0)\n"
    )
    out = pp.validate_policy_code(bad)
    assert not out["ok"]
    assert any("安全校验" in i for i in out["issues"])


def test_validate_rejects_missing_compute(tmp_path: Path) -> None:
    bad = 'META = {"id": "up_x", "label": "缺函数", "params": []}\n'
    out = pp.validate_policy_code(bad)
    assert not out["ok"]
    assert any("compute" in i for i in out["issues"])


def test_save_list_get_delete_cycle(tmp_path: Path) -> None:
    meta = dict(pp.TEMPLATE_VOL_TARGET["meta"], id="up_vol_test")
    saved = pp.save_policy(tmp_path, meta, pp.TEMPLATE_VOL_TARGET["code"])
    assert saved["ok"], saved["issues"]
    assert saved["id"] == "up_vol_test"
    views, errors = pp.list_policies(tmp_path)
    assert errors == {}
    assert [v["id"] for v in views] == ["up_vol_test"]
    policy = pp.get_policy(tmp_path, "up_vol_test")
    assert policy is not None
    out = smoke_compute(policy.compute)
    assert out["ok"], out["issues"]
    # 参数默认值注入后冒烟 (波动率倒数: 波幅大的权重小)
    from app.backtest.position_sizing import PositionSizingContext as Ctx

    ctx = synthetic_context(3)
    ctx2 = Ctx(
        selected=ctx.selected,
        recent_returns=ctx.recent_returns,
        closed_trades=ctx.closed_trades,
        portfolio=ctx.portfolio,
        params={"window": 60},
    )
    weights, _ = normalize_result(policy.compute(ctx2), 3)
    assert weights == sorted(weights, reverse=True)
    assert pp.delete_policy(tmp_path, "up_vol_test")
    assert pp.get_policy(tmp_path, "up_vol_test") is None


def test_broken_file_isolated_not_fatal(tmp_path: Path) -> None:
    d = tmp_path / pp.POLICY_DIR
    d.mkdir(parents=True)
    (d / "up_good.py").write_text(
        pp.render_policy_file(
            {"id": "up_good", "label": "好策略", "params": []},
            pp.TEMPLATE_BLANK["code"],
        ),
        encoding="utf-8",
    )
    (d / "broken.py").write_text("def (", encoding="utf-8")
    (d / "danger.py").write_text("import os\n", encoding="utf-8")
    views, errors = pp.list_policies(tmp_path)
    assert [v["id"] for v in views] == ["up_good"]
    assert set(errors) == {"broken.py", "danger.py"}


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    d = tmp_path / pp.POLICY_DIR
    d.mkdir(parents=True)
    code = pp.render_policy_file(
        {"id": "up_dup", "label": "重复", "params": []}, pp.TEMPLATE_BLANK["code"]
    )
    (d / "a.py").write_text(code, encoding="utf-8")
    (d / "b.py").write_text(code, encoding="utf-8")
    views, errors = pp.list_policies(tmp_path)
    assert [v["id"] for v in views] == ["up_dup"]
    assert "重复策略 id" in errors["b.py"]
