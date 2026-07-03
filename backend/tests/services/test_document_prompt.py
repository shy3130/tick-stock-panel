from app.services import document_reader, financial_analyzer, market_recap, stock_analyzer


def test_document_text_appended_to_ai_prompts(monkeypatch):
    monkeypatch.setattr(document_reader, "MAX_PROMPT_DOCUMENT_CHARS", 5)

    stock = stock_analyzer._build_user_prompt([], {}, {}, None, "600519.SH", "", "abcdef")
    financial = financial_analyzer._build_user_prompt({"metrics": [{"roe": 1}]}, "600519.SH", "", "abcdef")
    recap = market_recap._build_user_prompt({"as_of": "2026-07-03"}, [], "", "abcdef")

    for prompt in (stock, financial, recap):
        assert "用户附件摘要" in prompt
        assert "非行情事实" in prompt
        assert "abcde" in prompt
        assert "abcdef" not in prompt
