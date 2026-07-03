from app.capabilities import Cap, CapabilityDenied, CapabilityLimits, CapabilitySet


def test_import_from_neutral_module_and_capset_roundtrip():
    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits(batch=100)})

    assert capset.has(Cap.FINANCIAL)
    assert capset.limits(Cap.FINANCIAL).batch == 100


def test_denied_suggestion_is_provider_wording():
    exc = CapabilityDenied(Cap.FINANCIAL)
    assert "加购" not in exc.suggestion
    assert "数据源" in exc.suggestion
