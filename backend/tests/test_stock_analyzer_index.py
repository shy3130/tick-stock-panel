"""AI 分析 prompt 指数文案测试。"""
from app.services.stock_analyzer import _build_user_prompt


def test_user_prompt_index_no_financials():
    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-07-24", "close": 3000.0}],
        fins={"metrics": [], "income": []},
        levels={}, close=3000.0, symbol="000001.SH", focus="",
        asset_type="index",
    )
    assert "指数" in prompt
    assert "Free 模式" not in prompt  # 指数无财务是常态, 不走 Free 文案
