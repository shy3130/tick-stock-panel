"""Dev 页演示规则不得引用不存在的策略 id (#490)。

背景: 内置策略在去内置化后已全部下线, 但 `_DEMO_STRATEGY_RULES` 仍硬编码
`trend_breakout` / `macd_golden`。种子接口不校验就落盘 → 规则列表里显示
「已启用」, 运行时 `_match_strategy` 把 `unknown strategy` 异常吞成空列表,
于是规则永远不触发、日志也查不出原因。

修复: 落盘前用策略引擎校验, 不存在的跳过并在响应里列出 `skipped`。
"""
from app.api.monitor_rules import _DEMO_STRATEGY_RULES, _strategy_available


class _FakeEngine:
    def __init__(self, ids):
        self._ids = set(ids)

    def has(self, strategy_id: str) -> bool:
        return strategy_id in self._ids


class _BoomEngine:
    def has(self, strategy_id: str) -> bool:
        raise RuntimeError("engine exploded")


def test_available_only_for_registered_ids():
    eng = _FakeEngine({"my_custom"})
    assert _strategy_available(eng, "my_custom") is True
    assert _strategy_available(eng, "trend_breakout") is False


def test_missing_engine_is_not_available():
    # 引擎未装配时宁可不种, 也不种一条永远不触发的规则
    assert _strategy_available(None, "trend_breakout") is False


def test_engine_error_is_not_available():
    assert _strategy_available(_BoomEngine(), "trend_breakout") is False


def test_builtin_demo_ids_are_filtered_out_on_fresh_install():
    """全新安装(没有任何自建策略): 两条内置演示策略都应被跳过。"""
    eng = _FakeEngine(set())
    kept = [t for t in _DEMO_STRATEGY_RULES if _strategy_available(eng, t["strategy_id"])]
    assert kept == []


def test_demo_templates_still_seed_when_strategy_exists():
    """用户自建了同名/同 id 策略时, 演示规则应照常生成(不能被一刀切掉)。"""
    ids = {t["strategy_id"] for t in _DEMO_STRATEGY_RULES}
    eng = _FakeEngine(ids)
    kept = [t for t in _DEMO_STRATEGY_RULES if _strategy_available(eng, t["strategy_id"])]
    assert len(kept) == len(_DEMO_STRATEGY_RULES)
