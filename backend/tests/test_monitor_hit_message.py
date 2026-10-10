"""告警 message 以实际命中信号为主 + 自定义信号中文名解析。

修复前: signal/price/market 类告警的 message 拼的是规则全量条件 (8 个 "或" 连接),
看不出实际触发的是哪条; 自定义信号 (csg_/csgi_) 显示原始列名而非用户命名。
修复后: 有命中信号时 message 以「命中 信号A、信号B」开头; 无 truth 命中
(纯比较条件) 时回退原条件摘要; 自定义信号解析为用户命名。
"""
import json
import tempfile
from pathlib import Path

import polars as pl

from app.strategy.monitor import MonitorRuleEngine
from tests.signal_seeds import materialize_data_root

# 信号列已定义驱动: 标签 (signal_* → 中文名) 从种子定义目录解析,
# 不依赖运行目录的用户数据。
_SEED_ROOT = materialize_data_root(Path(tempfile.mkdtemp(prefix="tsp_sig_seeds_")))


def _engine() -> MonitorRuleEngine:
    eng = MonitorRuleEngine()
    eng.set_data_dir(_SEED_ROOT)
    return eng


def _rule(rid="r1", conditions=None, **over):
    rule = {
        "id": rid, "name": rid, "type": "signal", "asset_type": "stock",
        "scope": "symbols", "symbols": ["000001.SH"], "logic": "or",
        "conditions": conditions or [], "cooldown_seconds": 0, "enabled": True,
    }
    rule.update(over)
    return rule


def _df(**cols):
    base = {"symbol": ["000001.SH"], "close": [3000.0], "change_pct": [0.01]}
    base.update(cols)
    return pl.DataFrame(base)


def _fire(eng, rule, df):
    eng.set_rules([rule])
    events = eng.evaluate(df, asset_type="stock", reset_strategy_results=False)
    assert len(events) == 1
    return events[0]["message"]


def test_message_leads_with_hit_signals():
    """OR 规则部分命中: message 只含实际命中的信号, 不再列全量条件。"""
    eng = _engine()
    rule = _rule(conditions=[
        {"field": "signal_ma_dead_5_20", "op": "truth"},
        {"field": "signal_macd_dead", "op": "truth"},
        {"field": "signal_boll_breakout_upper", "op": "truth"},
    ])
    df = _df(signal_ma_dead_5_20=[True], signal_macd_dead=[True],
             signal_boll_breakout_upper=[False])
    msg = _fire(eng, rule, df)
    assert msg.startswith("命中 MA5下穿MA20、MACD死叉")
    assert "突破布林上轨" not in msg
    assert " 或 " not in msg
    assert "现价 3000.0" in msg


def test_message_falls_back_to_conditions_when_no_truth_hit():
    """纯比较条件规则 (hit_sigs 为空): 保持原条件摘要格式, 行为不变。"""
    eng = _engine()
    rule = _rule(type="price",
                 conditions=[{"field": "close", "op": ">=", "value": 2500}])
    msg = _fire(eng, rule, _df())
    assert "收盘价>=2500" in msg
    assert "现价 3000.0" in msg


def test_message_resolves_custom_signal_cn_name(tmp_path):
    """csg_ 自定义信号在 message 中显示用户命名, 而非原始列名。"""
    d = tmp_path / "user_data" / "custom_signals"
    d.mkdir(parents=True)
    (d / "ma_dead_5_10.json").write_text(json.dumps({
        "id": "ma_dead_5_10", "name": "5日线下穿10日线", "kind": "exit",
        "conditions": [{"left": "ma5", "op": "<=", "right": "field:ma10"}],
    }, ensure_ascii=False), encoding="utf-8")

    eng = _engine()
    eng.set_data_dir(tmp_path)
    rule = _rule(conditions=[{"field": "csg_ma_dead_5_10", "op": "truth"}])
    msg = _fire(eng, rule, _df(csg_ma_dead_5_10=[True]))
    assert "5日线下穿10日线" in msg
    assert "csg_ma_dead_5_10" not in msg


def test_message_and_logic_includes_comparison_conditions():
    """AND 规则 truth+比较混合: 比较条件全部满足, 一并补进 message (信息完整)。"""
    eng = _engine()
    rule = _rule(logic="and", conditions=[
        {"field": "signal_macd_dead", "op": "truth"},
        {"field": "close", "op": ">=", "value": 2500},
    ])
    msg = _fire(eng, rule, _df(signal_macd_dead=[True]))
    assert msg.startswith("命中 MACD死叉 且 收盘价>=2500")
    assert "现价 3000.0" in msg


def test_message_or_logic_omits_comparison_conditions():
    """OR 规则 truth+比较混合: 无法判定比较条件是否为真, 不补进 message。"""
    eng = _engine()
    rule = _rule(logic="or", conditions=[
        {"field": "signal_macd_dead", "op": "truth"},
        {"field": "close", "op": ">=", "value": 2500},
    ])
    msg = _fire(eng, rule, _df(signal_macd_dead=[True]))
    assert msg.startswith("命中 MACD死叉 · ")
    assert "收盘价" not in msg


def test_message_custom_signal_fallback_without_data_dir():
    """未注入 data_dir 时 csg_ 名称解析优雅回退为原始列名 (不报错)。"""
    eng = _engine()
    rule = _rule(conditions=[{"field": "csg_unknown_sig", "op": "truth"}])
    msg = _fire(eng, rule, _df(csg_unknown_sig=[True]))
    assert "csg_unknown_sig" in msg
