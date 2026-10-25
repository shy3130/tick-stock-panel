"""扩展表字段 → 因子/信号接入测试。

覆盖: 命名与数值过滤 / 注册表惰性同步 / 时序按日 PIT 对齐 / 快照仅单日帧
门控 / 自定义信号消费 / 评分引用 / 写入后缓存失效 / compute_signals 集成。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.factors import ext_factors
from app.factors.ext_factors import ext_factor_specs
from app.factors.registry import all_factors, get_factor
from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    ExtField,
    _load_all_cache,
    write_ext_parquet,
)
from app.strategy import custom_signals

COL = "ext_tags_hot"  # config_id=tags, field=hot


@pytest.fixture(autouse=True)
def _clean_caches():
    _load_all_cache.clear()
    ext_factors._frame_cache.clear()
    ext_factors._sync_state = None
    yield
    _load_all_cache.clear()
    ext_factors._frame_cache.clear()
    ext_factors._sync_state = None
    # 清理注册表里残留的 ext_ 条目, 不污染其他测试。
    # 直接遍历 _REGISTRY 而不经 all_factors() — 后者会触发惰性同步,
    # 在 monkeypatch 已还原后把真实数据目录的配置注册进来。
    from app.factors import registry as _registry

    for fid in [k for k in list(_registry._REGISTRY) if k.startswith(ext_factors.EXT_PREFIX)]:
        _registry.unregister_factor(fid)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """统一把 settings.data_dir 指向 tmp (ext_factors/registry 默认目录解析)。"""
    from app import config as app_config

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    return tmp_path


def _mk_config(data_dir, cid="tags", mode="timeseries", fields=None):
    cfg = ExtConfig(
        id=cid, label="题材标签", mode=mode,
        fields=fields or [
            ExtField(name="hot", dtype="float"),
            ExtField(name="cnt", dtype="int"),
            ExtField(name="name", dtype="string"),  # 非数值: 不应暴露
        ],
    )
    ExtConfigStore(data_dir).upsert(cfg)
    return cfg


def _frame(rows) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={"symbol": pl.Utf8, "date": pl.Utf8, "close": pl.Float64},
        orient="row",
    ).sort(["symbol", "date"])


# ── 命名与注册 ────────────────────────────────────────────

def test_column_name_sanitization():
    assert ext_factors.ext_column_name("tags", "hot") == "ext_tags_hot"
    assert ext_factors.ext_column_name("tags", "a-b c") == "ext_tags_a_b_c"  # 非单词字符转下划线
    # 中文字段名保留 (预设表字段多为中文, 折叠会互相碰撞)
    assert ext_factors.ext_column_name("tags", "所属概念") == "ext_tags_所属概念"
    assert ext_factors.ext_column_name("tags", "所属概念") != ext_factors.ext_column_name("tags", "股票简称")


def test_numeric_only_specs(data_dir):
    _mk_config(data_dir)
    specs = ext_factors.ext_factor_specs(data_dir)
    ids = {s.id for s in specs}
    assert "ext_tags_hot" in ids and "ext_tags_cnt" in ids
    assert "ext_tags_name" not in ids  # string 字段不进入数值口径
    # 中文名的数值字段: 列照常 join, 但不注册因子 (DSL 标识符 ASCII-only)
    _mk_config(data_dir, cid="cn1", mode="snapshot",
               fields=[ExtField(name="涨停数", dtype="int")])
    cn_specs = {s.id for s in ext_factors.ext_factor_specs(data_dir)}
    assert ext_factors.ext_column_name("cn1", "涨停数") == "ext_cn1_涨停数"
    assert "ext_cn1_涨停数" not in cn_specs
    spec = next(s for s in specs if s.id == "ext_tags_hot")
    assert spec.kind == "base" and not spec.dependencies  # 已物化列自身
    assert spec.group == "扩展数据"


def test_ensure_synced_registers_and_unregisters(data_dir):
    _mk_config(data_dir)
    assert get_factor(COL) is None
    ext_factors.ensure_synced(data_dir)
    assert get_factor(COL) is not None
    assert COL in custom_signals.allowed_fields()  # 信号字段白名单自动并入
    # 删除配置 → 下次同步注销
    ExtConfigStore(data_dir).delete("tags")
    ext_factors.invalidate_ext_caches(data_dir)
    ext_factors.ensure_synced(data_dir)
    assert get_factor(COL) is None
    assert COL not in custom_signals.allowed_fields()


def test_all_factors_lazy_sync(data_dir):
    _mk_config(data_dir)
    assert COL in {s.id for s in all_factors()}  # all_factors 内部惰性同步


# ── 时序按日对齐 (PIT) ────────────────────────────────────

def test_timeseries_exact_date_alignment(data_dir):
    cfg = _mk_config(data_dir, mode="timeseries")
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH"], "hot": [0.9]}),
        cfg, data_dir, snapshot_date=date(2026, 1, 5),
    )
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "hot": [0.2, 0.7]}),
        cfg, data_dir, snapshot_date=date(2026, 1, 6),
    )
    frame = _frame([
        ("600000.SH", "2026-01-05", 10.0),
        ("600000.SH", "2026-01-06", 11.0),
        ("600000.SH", "2026-01-07", 12.0),  # 无分区 → null
        ("000001.SZ", "2026-01-06", 20.0),
        ("000001.SZ", "2026-01-07", 21.0),  # 无分区 → null
    ])
    out = ext_factors.attach_ext_columns(frame, include_snapshot=False, data_dir=data_dir)
    assert COL in out.columns
    by_key = {(r[0], r[1]): r[2] for r in out.select("symbol", "date", COL).rows()}
    assert by_key[("600000.SH", "2026-01-05")] == 0.9
    assert by_key[("600000.SH", "2026-01-06")] == 0.2
    assert by_key[("000001.SZ", "2026-01-06")] == 0.7
    assert by_key[("600000.SH", "2026-01-07")] is None  # 缺分区 → null, 不前视填充
    assert by_key[("000001.SZ", "2026-01-07")] is None


def test_timeseries_no_lookahead_across_dates(data_dir):
    """历史帧只能看到各日期自己的值: d2 的高值不得泄露到 d1 行。"""
    cfg = _mk_config(data_dir, mode="timeseries")
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH"], "hot": [0.1]}),
        cfg, data_dir, snapshot_date=date(2026, 1, 5),
    )
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH"], "hot": [9.9]}),
        cfg, data_dir, snapshot_date=date(2026, 1, 6),
    )
    frame = _frame([
        ("600000.SH", "2026-01-05", 10.0),
        ("600000.SH", "2026-01-06", 11.0),
    ])
    out = ext_factors.attach_ext_columns(frame, include_snapshot=False, data_dir=data_dir)
    assert out[COL].to_list() == [0.1, 9.9]


# ── 快照门控 ──────────────────────────────────────────────

def test_snapshot_gated_off_history_frames(data_dir):
    cfg = _mk_config(data_dir, mode="snapshot")
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH"], "hot": [1.5]}),
        cfg, data_dir, snapshot_date=date(2026, 1, 6),
    )
    hist = _frame([
        ("600000.SH", "2026-01-05", 10.0),
        ("600000.SH", "2026-01-06", 11.0),
    ])
    out = ext_factors.attach_ext_columns(hist, include_snapshot=False, data_dir=data_dir)
    assert COL not in out.columns  # 多日历史帧禁止注入快照 (未来函数)

    today = _frame([("600000.SH", "2026-01-06", 11.0)])
    out2 = ext_factors.attach_ext_columns(today, include_snapshot=True, data_dir=data_dir)
    assert COL in out2.columns and out2[COL].to_list() == [1.5]


# ── 信号消费 / 评分引用 ───────────────────────────────────

def _save_signal(data_dir, sid="ext_hot", left=COL):
    custom_signals.save_one(data_dir, {
        "id": sid, "name": "题材热度", "kind": "entry", "enabled": True,
        "conditions": [{"left": left, "op": ">", "right": "0.5", "leftDays": 0, "rightDays": 0}],
    })


def test_signal_validate_and_inject_with_ext_field(data_dir):
    _mk_config(data_dir, mode="timeseries")
    _save_signal(data_dir)
    sig = custom_signals.load_all(data_dir)[0]
    custom_signals.validate(sig)  # ext 字段在白名单 → 不抛错

    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "hot": [0.9, 0.2]}),
        ExtConfigStore(data_dir).get("tags"), data_dir, snapshot_date=date(2026, 1, 5),
    )
    frame = _frame([
        ("600000.SH", "2026-01-05", 10.0),
        ("000001.SZ", "2026-01-05", 20.0),
    ])
    # 与 compute_signals 相同顺序: 先 attach 扩展列, 再编译注入
    frame = ext_factors.attach_ext_columns(frame, include_snapshot=False, data_dir=data_dir)
    exprs = custom_signals.build_expressions([sig])
    out = custom_signals.inject(frame, exprs)
    # 排序后 000001.SZ (hot=0.2) 在前, 600000.SH (hot=0.9) 在后
    assert out["csg_ext_hot"].to_list() == [False, True]


def test_scoring_value_expr_resolves_ext_column():
    from app.strategy.scoring import scoring_value_expr

    assert scoring_value_expr(["symbol", COL], COL) is not None  # 列存在 → 直接引用
    assert scoring_value_expr(["symbol"], COL) is None  # 缺列 → 不可计算 (非伪装零分)


# ── 失效链路 ──────────────────────────────────────────────

def test_write_invalidates_frame_cache(data_dir):
    cfg = _mk_config(data_dir, mode="timeseries")
    frame = _frame([("600000.SH", "2026-01-05", 10.0)])
    out1 = ext_factors.attach_ext_columns(frame, include_snapshot=False, data_dir=data_dir)
    assert COL not in out1.columns  # 尚无数据 → 不产列 (引用方按缺列优雅降级)
    # write_ext_parquet 内部调用 _invalidate_ext_derived → 帧缓存失效
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH"], "hot": [0.8]}),
        cfg, data_dir, snapshot_date=date(2026, 1, 5),
    )
    out2 = ext_factors.attach_ext_columns(frame, include_snapshot=False, data_dir=data_dir)
    assert out2[COL].to_list() == [0.8]


def test_routine_pull_keeps_strategy_cache_but_default_clears(data_dir):
    """定时拉取 (keep_strategy_cache=True) 只失效扩展帧缓存, 不销毁策略结果。

    策略页依赖 strategy_cache 秒加载; 周期性拉取每轮全清会让页面在两次
    全量重算之间整页空白。手动上传/配置变更 (默认路径) 保持全清旧行为。
    """
    from app.services import strategy_cache

    cfg = _mk_config(data_dir, mode="timeseries")
    strategy_cache.write_cache(
        data_dir, "2026-01-05",
        {"s1": {"total": 1, "as_of": "2026-01-05", "rows": []}},
    )

    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH"], "hot": [0.8]}),
        cfg, data_dir, snapshot_date=date(2026, 1, 5),
        keep_strategy_cache=True,
    )
    # 策略结果保留; 扩展帧缓存仍失效 → 新值立即可见
    cached = strategy_cache.read_cache(data_dir) or {}
    assert cached.get("results", {}).get("s1", {}).get("total") == 1
    frame = _frame([("600000.SH", "2026-01-05", 10.0)])
    out = ext_factors.attach_ext_columns(frame, include_snapshot=False, data_dir=data_dir)
    assert out[COL].to_list() == [0.8]

    # 默认路径 (手动写入): 全清
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH"], "hot": [0.9]}),
        cfg, data_dir, snapshot_date=date(2026, 1, 5),
    )
    assert strategy_cache.read_cache(data_dir) is None


def test_scheduler_status_upsert_keeps_strategy_cache(data_dir):
    """定时拉取循环的 last_run/next_run 例行回写 (keep_strategy_cache=True)
    不清策略结果缓存; UI 保存配置 (默认) 仍全清。

    线上事故: 每轮拉取成功后 store.upsert(fresh) 状态回写触发全清, 数据
    写入链路放行后 12ms 缓存仍被清空 —— 拉取循环内所有回写都须放行。
    """
    from app.services import strategy_cache
    from app.services.ext_data import ExtConfigStore

    cfg = _mk_config(data_dir, mode="timeseries")
    store = ExtConfigStore(data_dir)
    strategy_cache.write_cache(
        data_dir, "2026-01-05",
        {"s1": {"total": 1, "as_of": "2026-01-05", "rows": []}},
    )

    # 调度器例行回写 (last_run/next_run): 保留策略结果
    store.upsert(cfg, keep_strategy_cache=True)
    cached = strategy_cache.read_cache(data_dir) or {}
    assert cached.get("results", {}).get("s1", {}).get("total") == 1

    # UI 保存配置 (字段集可能变化): 默认全清
    store.upsert(cfg)
    assert strategy_cache.read_cache(data_dir) is None


def test_config_field_change_invalidates_sync(data_dir):
    _mk_config(data_dir)
    ext_factors.ensure_synced(data_dir)
    assert get_factor(COL) is not None
    # 改字段集 (去掉 hot): upsert 触发失效, 再同步后注销
    cfg2 = ExtConfig(
        id="tags", label="题材标签", mode="timeseries",
        fields=[ExtField(name="cnt", dtype="int")],
    )
    ExtConfigStore(data_dir).upsert(cfg2)
    ext_factors.ensure_synced(data_dir)
    assert get_factor(COL) is None
    assert get_factor("ext_tags_cnt") is not None


# ── compute_signals 集成 (历史路径) ───────────────────────

def test_compute_signals_attaches_ext_columns(data_dir):
    from app.indicators import pipeline

    pipeline.invalidate_custom_signals()
    _mk_config(data_dir, mode="timeseries")
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH"], "hot": [0.9]}),
        ExtConfigStore(data_dir).get("tags"), data_dir, snapshot_date=date(2026, 1, 5),
    )
    _save_signal(data_dir)
    # 快照配置即使存在也不得进入历史帧
    snap = _mk_config(data_dir, cid="snap1", mode="snapshot")
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH"], "hot": [1.5]}),
        snap, data_dir, snapshot_date=date(2026, 1, 6),
    )

    frame = _frame([("600000.SH", "2026-01-05", 10.0)])
    try:
        out = pipeline.compute_signals(frame, needed={"csg_ext_hot"})
    finally:
        pipeline.invalidate_custom_signals()
    assert COL in out.columns
    assert "ext_snap1_hot" not in out.columns  # 历史帧快照门控
    assert out["csg_ext_hot"].to_list() == [True]


# ── string 扩展字段 (概念/行业归属) ───────────────────────

STR_COL = "ext_tags_cat"  # config_id=tags, string 字段 cat


def _mk_str_config(data_dir):
    return _mk_config(data_dir, fields=[
        ExtField(name="hot", dtype="float"),
        ExtField(name="cat", dtype="string"),  # 归属字段, 分号拼接
    ])


def test_string_fields_exposed_for_signals_only(data_dir):
    _mk_str_config(data_dir)
    entries = ext_factors.ext_string_field_entries(data_dir)
    assert {e["key"] for e in entries} == {STR_COL}
    assert STR_COL in ext_factors.ext_string_fields(data_dir)
    assert STR_COL in custom_signals.allowed_fields()
    # 不注册为因子: IC/排序是数值口径
    assert get_factor(STR_COL) is None
    assert STR_COL not in {s.id for s in ext_factor_specs(data_dir)}


def test_string_validate_accepts_and_rejects(data_dir):
    _mk_str_config(data_dir)
    ok = {
        "id": "t_cat", "name": "概念归属", "kind": "entry",
        "conditions": [{"left": STR_COL, "op": "contains", "right": "AI", "leftDays": 0, "rightDays": 0}],
    }
    custom_signals.validate(ok)  # contains + 字符串字面量

    def _bad(**patch):
        c = dict(left=STR_COL, op="contains", right="AI", leftDays=0, rightDays=0)
        c.update(patch)
        return {"id": "t_bad", "name": "x", "kind": "entry", "conditions": [c]}

    with pytest.raises(ValueError, match="仅支持"):
        custom_signals.validate(_bad(op=">"))  # 字符串字段禁用数值运算符
    with pytest.raises(ValueError, match="非空字符串"):
        custom_signals.validate(_bad(right="  "))
    with pytest.raises(ValueError, match="不支持字段引用"):
        custom_signals.validate(_bad(right="field:close"))
    with pytest.raises(ValueError, match="contains 仅用于字符串"):
        custom_signals.validate({  # 数值字段禁用 contains
            "id": "t_bad2", "name": "x", "kind": "entry",
            "conditions": [{"left": "close", "op": "contains", "right": "AI", "leftDays": 0, "rightDays": 0}],
        })


def test_string_contains_inject_semantics(data_dir):
    _mk_str_config(data_dir)
    write_ext_parquet(
        pl.DataFrame({
            "symbol": ["600000.SH", "000001.SZ", "300750.SZ"],
            "cat": ["AI;芯片", "半导体", None],  # null: 无归属
        }),
        ExtConfigStore(data_dir).get("tags"), data_dir, snapshot_date=date(2026, 1, 5),
    )
    sig = {
        "id": "cat_ai", "name": "AI题材", "kind": "entry", "enabled": True,
        "conditions": [{"left": STR_COL, "op": "contains", "right": "AI", "leftDays": 0, "rightDays": 0}],
    }
    frame = _frame([
        ("300750.SZ", "2026-01-05", 10.0),  # null 归属 → 不命中
        ("600000.SH", "2026-01-05", 11.0),
        ("000001.SZ", "2026-01-05", 20.0),
    ])
    frame = ext_factors.attach_ext_columns(frame, include_snapshot=False, data_dir=data_dir)
    out = custom_signals.inject(frame, custom_signals.build_expressions([sig]))
    got = dict(zip(out["symbol"], out["csg_cat_ai"], strict=True))
    assert got["600000.SH"] is True       # "AI;芯片" 包含 AI
    assert not got["000001.SZ"]           # "半导体" 不含
    assert not got["300750.SZ"]           # null → 不误报


def test_string_contains_is_literal_not_regex(data_dir):
    """右值按字面量匹配: '.' 不当正则万能匹配。"""
    _mk_str_config(data_dir)
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH"], "cat": ["AI;芯片"]}),
        ExtConfigStore(data_dir).get("tags"), data_dir, snapshot_date=date(2026, 1, 5),
    )
    sig_dot = {
        "id": "cat_dot", "name": "x", "kind": "entry", "enabled": True,
        "conditions": [{"left": STR_COL, "op": "contains", "right": ".", "leftDays": 0, "rightDays": 0}],
    }
    frame = _frame([("600000.SH", "2026-01-05", 10.0)])
    frame = ext_factors.attach_ext_columns(frame, include_snapshot=False, data_dir=data_dir)
    out = custom_signals.inject(frame, custom_signals.build_expressions([sig_dot]))
    assert out["csg_cat_dot"].to_list() == [False]  # 正则下 '.' 会匹配任意字符


def test_string_equals_and_mixed_conditions(data_dir):
    _mk_str_config(data_dir)
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "cat": ["半导体", "银行"], "hot": [0.9, 0.2]}),
        ExtConfigStore(data_dir).get("tags"), data_dir, snapshot_date=date(2026, 1, 5),
    )
    sig = {
        "id": "semi_strong", "name": "强势半导体", "kind": "entry", "enabled": True,
        "conditions": [
            {"left": STR_COL, "op": "==", "right": "半导体", "leftDays": 0, "rightDays": 0},
            {"left": COL, "op": ">", "right": "0.5", "leftDays": 0, "rightDays": 0},  # 混合: 归属且热度
        ],
    }
    frame = _frame([
        ("600000.SH", "2026-01-05", 10.0),
        ("000001.SZ", "2026-01-05", 20.0),
    ])
    frame = ext_factors.attach_ext_columns(frame, include_snapshot=False, data_dir=data_dir)
    out = custom_signals.inject(frame, custom_signals.build_expressions([sig]))
    got = dict(zip(out["symbol"], out["csg_semi_strong"], strict=True))
    assert got["600000.SH"] is True
    assert not got["000001.SZ"]


def test_cjk_string_field_end_to_end(data_dir):
    """预设表场景: 中文字段名 (所属概念) 列名保留中文, 信号全链路可用。"""
    ExtConfigStore(data_dir).upsert(ExtConfig(
        id="ths_concepts", label="扩展概念", mode="snapshot",
        fields=[ExtField(name="所属概念", dtype="string")],
    ))
    write_ext_parquet(
        pl.DataFrame({"symbol": ["600000.SH"], "所属概念": ["AI芯片;机器人"]}),
        ExtConfigStore(data_dir).get("ths_concepts"), data_dir, snapshot_date=date(2026, 1, 6),
    )
    col = "ext_ths_concepts_所属概念"
    assert col in custom_signals.allowed_fields()
    sig = {
        "id": "robot", "name": "机器人题材", "kind": "entry", "enabled": True,
        "conditions": [{"left": col, "op": "contains", "right": "机器人", "leftDays": 0, "rightDays": 0}],
    }
    custom_signals.validate(sig)
    frame = _frame([("600000.SH", "2026-01-06", 10.0)])
    frame = ext_factors.attach_ext_columns(frame, include_snapshot=True, data_dir=data_dir)
    assert col in frame.columns
    out = custom_signals.inject(frame, custom_signals.build_expressions([sig]))
    assert out["csg_robot"].to_list() == [True]


# ── worker 子进程注册表同步 (线上回归) ────────────────────

def test_mining_worker_registry_must_include_ext_factors(data_dir):
    """回归: 挖掘 worker 只 load_into_registry (uf_/cf_) 时注册表缺 ext 因子,
    _decode_runtime_request 直接报 unknown mining factors (线上实测)。

    worker 入口在 load_into_registry 后必须再 ensure_synced; 这里锁定两个状态
    的 factor_columns_view 差异, 防止未来把 ensure_synced 从 worker 里删掉。
    """
    from app.backtest.mining_runtime import _decode_runtime_request
    from app.factors.registry import factor_columns_view
    from app.factors.store import load_into_registry

    # 配置 id 本身带 ext_ 前缀 → 列名双前缀 (与线上报错的 id 形态一致)
    _mk_config(data_dir, cid="ext_auction")
    col = ext_factors.ext_column_name("ext_auction", "hot")
    assert col == "ext_ext_auction_hot"

    # 旧 worker 行为: 只加载 uf_/cf_ → 校验拒绝
    load_into_registry(data_dir)
    assert col not in {item["id"] for item in factor_columns_view()}
    payload = {"run_id": "r1", "request": {"factor_names": [col]}}
    with pytest.raises(ValueError, match="unknown mining factors"):
        _decode_runtime_request(payload, data_dir, strategy_engine=None)

    # 修复后的 worker 行为: 同步 ext 因子 → 校验放行 (后续解码错误不算失败)
    ext_factors.ensure_synced(data_dir)
    assert col in {item["id"] for item in factor_columns_view()}
    try:
        _decode_runtime_request(payload, data_dir, strategy_engine=None)
    except ValueError as exc:
        assert "unknown mining factors" not in str(exc)
