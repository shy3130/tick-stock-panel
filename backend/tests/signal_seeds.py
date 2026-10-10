"""测试用「迁移信号定义」注入 — 与 data/user_data/custom_signals 种子同语义。

信号列已全部定义驱动: 干净环境 (CI / 空数据目录) 下引用 signal_* 列的
测试必须自带定义, 不依赖运行目录的用户数据。种子 JSON 收录在
tests/fixtures/custom_signals/ (入库, 仅测试使用)。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "custom_signals"


def load_seed_definitions() -> list[dict]:
    """单一事实源: app.strategy.signal_seeds (fixtures JSON 与其同内容)。"""
    from app.strategy.signal_seeds import SEED_SIGNALS

    return SEED_SIGNALS


def install_pipeline_caches(monkeypatch) -> list[dict]:
    """把种子定义编译进 pipeline 的表达式缓存 (全量 + 当日两套)。

    覆盖 compute_signals / compute_limit_signals 注入与
    get_signal_dependencies() 依赖解析; monkeypatch 结束自动还原。
    """
    from app.indicators import pipeline
    from app.strategy import custom_signals

    sigs = load_seed_definitions()
    prev_fields = custom_signals.prev_day_fields(sigs)
    monkeypatch.setattr(
        pipeline, "_custom_signal_exprs", custom_signals.build_expressions(sigs)
    )
    monkeypatch.setattr(
        pipeline,
        "_custom_signal_exprs_today",
        custom_signals.build_expressions_prev(sigs, prev_fields),
    )
    monkeypatch.setattr(pipeline, "_custom_prev_fields_cache", prev_fields)
    return sigs


def materialize_data_root(target: Path) -> Path:
    """把种子 JSON 铺成 <target>/user_data/custom_signals/*.json 目录结构。

    供按 data_dir 读定义的调用方 (MonitorRuleEngine._signal_label 等) 使用。
    """
    d = target / "user_data" / "custom_signals"
    d.mkdir(parents=True, exist_ok=True)
    for p in sorted(FIXTURE_DIR.glob("*.json")):
        shutil.copy(p, d / p.name)
    return target
