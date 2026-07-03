from app.capabilities import Cap, CapabilityDenied


def test_denied_suggestion_is_provider_wording():
    exc = CapabilityDenied(Cap.FINANCIAL)
    assert "加购" not in exc.suggestion
    assert "数据源" in exc.suggestion
