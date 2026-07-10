"""优化器 API job_key 契约测试 — 守护 stream 与 cancel 的 key 对齐 (仿 PR3 C1 教训)。"""
from __future__ import annotations

from app.api.backtest import _OPT_BT_FIELDS, _make_opt_job_key, _opt_backtest_kwargs


def _sig(bt: dict) -> str:
    return "|".join(f"{k}={bt[k]}" for k in _OPT_BT_FIELDS)


def test_job_key_deterministic():
    bt = _opt_backtest_kwargs("open_t+1", 0.0002, None, None, 5.0, 10, 1.0, 1e6, "equal", "position", 5)
    sig = _sig(bt)
    k1 = _make_opt_job_key("s", None, None, None, '{"p":[1,2]}', "sortino", None, sig)
    k2 = _make_opt_job_key("s", None, None, None, '{"p":[1,2]}', "sortino", None, sig)
    assert k1 == k2


def test_job_key_distinguishes_grid_and_objective():
    bt = _opt_backtest_kwargs("open_t+1", 0.0002, None, None, 5.0, 10, 1.0, 1e6, "equal", "position", 5)
    sig = _sig(bt)
    base = _make_opt_job_key("s", None, None, None, '{"p":[1,2]}', "sortino", None, sig)
    assert base != _make_opt_job_key("s", None, None, None, '{"p":[1,3]}', "sortino", None, sig)  # grid 不同
    assert base != _make_opt_job_key("s", None, None, None, '{"p":[1,2]}', "sharpe", None, sig)   # objective 不同


def test_cancel_looks_up_job_by_echoed_key():
    """重构后: cancel 直接用 stream 回吐的 job_key 查表, 不再重算参数。

    这消除了'两侧重算必须逐字段一致'的脆弱契约 (PR3 C1 / direction 空串失配的根因)。
    """
    import asyncio

    from app.api.backtest import _BacktestJob, _running_jobs, optimize_cancel

    class _Req:
        def __init__(self, body):
            self._body = body
        async def json(self):
            return self._body

    key = "optkey_test_1"
    job = _BacktestJob(key)
    _running_jobs[key] = job
    try:
        # 用回吐的 key 取消 → 命中并 set cancel_event
        res = asyncio.run(optimize_cancel(_Req({"job_key": key})))
        assert res["ok"] is True
        assert job.cancel_event.is_set()

        # 已完成任务再取消 → ok False
        job.done = True
        res2 = asyncio.run(optimize_cancel(_Req({"job_key": key})))
        assert res2["ok"] is False

        # 未知 key → ok False, 不抛异常
        res3 = asyncio.run(optimize_cancel(_Req({"job_key": "nonexistent"})))
        assert res3["ok"] is False
    finally:
        _running_jobs.pop(key, None)
