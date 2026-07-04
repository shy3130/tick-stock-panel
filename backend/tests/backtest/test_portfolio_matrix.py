import numpy as np

from app.backtest.portfolio import momentum_from_prices, returns_from_prices


def test_returns_from_prices_basic():
    prices = np.array([[10.0, 100.0], [11.0, 90.0], [11.0, 99.0]])
    rets = returns_from_prices(prices)

    assert rets.shape == (2, 2)
    np.testing.assert_allclose(rets[0], [0.1, -0.1])
    np.testing.assert_allclose(rets[1], [0.0, 0.1])


def test_returns_from_prices_too_short():
    assert returns_from_prices(np.array([[10.0, 100.0]])).shape == (0, 2)


def test_momentum_from_prices():
    prices = np.array([[10.0, 100.0], [11.0, 90.0], [12.0, 99.0]])
    np.testing.assert_allclose(momentum_from_prices(prices), [0.2, -0.01])
