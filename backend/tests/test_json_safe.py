from __future__ import annotations

import json
from datetime import date

import numpy as np

from app.json_safe import finite_float_or_none, json_safe
from app.services.backtest import _json_safe as backtest_json_safe


def test_finite_float_or_none_rejects_non_finite_and_bool():
    assert finite_float_or_none(1.25) == 1.25
    assert finite_float_or_none("2.5") == 2.5
    assert finite_float_or_none(float("nan")) is None
    assert finite_float_or_none(float("inf")) is None
    assert finite_float_or_none(True) is None


def test_json_safe_recursively_removes_non_finite_numbers():
    payload = {
        "stats": {
            "nan": float("nan"),
            "pos_inf": np.float64(float("inf")),
            "neg_inf": -float("inf"),
            "ok": np.float64(0.5),
        },
        "curve": [1.0, float("nan")],
        "date": date(2026, 8, 10),
    }
    safe = json_safe(payload)
    assert safe["stats"] == {"nan": None, "pos_inf": None, "neg_inf": None, "ok": 0.5}
    assert safe["curve"] == [1.0, None]
    assert safe["date"] == "2026-08-10"
    json.dumps(safe, allow_nan=False)


def test_backtest_scalar_json_safe_rejects_both_nan_and_inf():
    assert backtest_json_safe(np.float64(float("nan"))) is None
    assert backtest_json_safe(np.float64(float("inf"))) is None
    assert backtest_json_safe(float("-inf")) is None
