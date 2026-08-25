from datetime import date

import pytest

from app.api.backtest import _segment_windows


def test_windows_cover_range_without_overlap():
    windows = _segment_windows(date(2024, 1, 1), date(2024, 12, 31), n_segments=4)
    assert len(windows) == 4
    assert windows[0][0] == date(2024, 1, 1)
    assert windows[-1][1] == date(2024, 12, 31)
    for (_s1, e1), (s2, _e2) in zip(windows, windows[1:]):
        assert e1 < s2


def test_windows_min_fold_length_guard():
    with pytest.raises(ValueError, match="窗口过短"):
        _segment_windows(date(2024, 1, 1), date(2024, 2, 1), n_segments=4)
