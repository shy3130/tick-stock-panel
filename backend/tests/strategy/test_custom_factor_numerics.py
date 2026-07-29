from __future__ import annotations

import warnings

import numpy as np
import pytest

from app.strategy.builtin.custom_factor import compute_features as custom_features
from app.strategy.builtin.factor_ensemble import compute_features as ensemble_features
from app.strategy.builtin.regime_conditional import compute_features as regime_features


@pytest.mark.parametrize("compute_features", [custom_features, ensemble_features, regime_features])
def test_factor_features_keep_missing_turnover_explicit_without_warnings(compute_features):
    size = 80
    close = np.linspace(10.0, 20.0, size)
    volume = np.linspace(1_000.0, 2_000.0, size)

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        features = compute_features(
            close,
            close,
            close + 0.2,
            close - 0.2,
            volume,
            np.full(size, np.nan),
        )

    assert np.isnan(features["TURN"]).all()
    assert np.isfinite(features["VOL_RATIO"][4:]).all()
    assert np.isfinite(features["MA60_DEV"][59:]).all()
