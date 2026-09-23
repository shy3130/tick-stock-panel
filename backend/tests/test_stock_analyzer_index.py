"""AI 分析 prompt 指数 / ETF 文案测试。"""
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


def test_user_prompt_etf_no_financials():
    """ETF 无上市公司财务是常态, 不得走股票 Free/未同步文案。

    指数分支已单独处理, ETF 若落入 else, 提示词会写成「Free 模式或尚未
    同步财务报表」, 模型按系统提示词第 4 节输出「财务接入中」, 用户会以为
    是套餐或同步问题, 而不是场内基金本来就没有公司报表。
    """
    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-07-24", "close": 4.12}],
        fins={"metrics": [], "income": []},
        levels={}, close=4.12, symbol="510300.SH", focus="",
        asset_type="etf",
    )
    assert "ETF" in prompt
    assert "Free 模式" not in prompt
    assert "尚未同步财务报表" not in prompt
