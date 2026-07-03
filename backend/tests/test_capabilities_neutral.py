from app.capabilities import Cap, CapabilityDenied


def test_compat_shim_points_to_same_cap():
    from app.tickflow.capabilities import Cap as OldCap

    assert OldCap is Cap


def test_denied_suggestion_is_provider_wording():
    exc = CapabilityDenied(Cap.FINANCIAL)
    assert "加购" not in exc.suggestion
    assert "数据源" in exc.suggestion
