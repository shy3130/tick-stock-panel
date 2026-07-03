import numpy as np

from app.backtest.optimizers import portfolio_weights


def test_portfolio_optimizers_return_long_only_normalized_weights():
    returns = np.array([
        [0.01, 0.02, -0.01],
        [0.02, -0.01, 0.01],
        [-0.01, 0.01, 0.02],
        [0.03, 0.00, 0.01],
    ])

    for method in ("equal_vol", "risk_parity", "mean_variance", "max_diversification"):
        weights = portfolio_weights(returns, method)
        assert np.isclose(weights.sum(), 1.0)
        assert np.all(weights >= 0)


def test_score_weight_keeps_existing_score_semantics():
    weights = portfolio_weights(np.zeros((2, 3)), "score_weight", np.array([0.0, 2.0, 6.0]))

    assert np.allclose(weights, [0.0, 0.25, 0.75])


def test_risk_parity_balances_risk_contribution_for_uneven_vol():
    rng = np.random.default_rng(7)
    returns = rng.normal(size=(400, 2)) * np.array([0.01, 0.5])

    weights = portfolio_weights(returns, "risk_parity")
    cov = np.cov(returns, rowvar=False) + np.eye(2) * 1e-8
    contribution = weights * (cov @ weights)

    assert weights[0] > 0.9
    assert not np.allclose(weights, [0.5, 0.5])
    assert np.allclose(contribution / contribution.sum(), [0.5, 0.5], atol=0.02)
