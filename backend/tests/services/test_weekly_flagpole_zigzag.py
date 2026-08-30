import pytest

from app.services.swing_zigzag import KIND_HIGH, confirmed_zigzag


def test_confirmation_latency_and_boundary():
    piv = confirmed_zigzag([10, 12, 12, 11.4, 13], [9, 11, 11, 10, 12], 0.05)
    assert piv and piv[0].kind == KIND_HIGH and piv[0].confirm_index > piv[0].index
    assert confirmed_zigzag([10, 10.5, 10.5], [9, 9.8, 9.975], 0.05)


def test_truncation_invariant():
    highs = [10, 12, 14, 13, 11, 15, 17, 16, 13]
    lows = [9, 11, 13, 12, 10, 14, 16, 15, 12]
    full = confirmed_zigzag(highs, lows, 0.1)
    for n in range(1, len(highs) + 1):
        prefix = confirmed_zigzag(highs[:n], lows[:n], 0.1)
        assert prefix == [p for p in full if p.confirm_index < n]


def test_invalid_inputs_fail_closed():
    with pytest.raises(ValueError):
        confirmed_zigzag([1], [1], 0)
    with pytest.raises(ValueError):
        confirmed_zigzag([1, 2], [1], 0.1)
