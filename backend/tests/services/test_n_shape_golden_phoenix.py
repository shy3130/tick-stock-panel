from datetime import date, timedelta

import polars as pl
import pytest

from app.services.n_shape_golden_phoenix import (
    FORWARD_HORIZONS,
    LOW_POSITION_MAX,
    VOLUME_BREAKOUT_RATIO,
    evaluate_n_shape,
    limit_up_price,
    resolve_pinned_reader,
)


def test_missing_generation_or_pit_is_unavailable():
    result = evaluate_n_shape(
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        symbols=["600000.SH"],
        pinned_reader=None,
        pit_provider=None,
    )
    assert result["status"] == "unavailable"
    assert set(result["unavailable_reasons"]) == {
        "generation_pinned_reader_missing",
        "pit_regime_st_missing",
    }
    assert result["events"] == []


def test_frozen_parameter_contract():
    assert LOW_POSITION_MAX == 0.35
    assert VOLUME_BREAKOUT_RATIO == 1.5
    assert FORWARD_HORIZONS == (1, 5, 10, 20)
    assert limit_up_price(10.00, 0.10) == 11.00


def test_missing_raw_columns_are_censored():
    from app.services.n_shape_golden_phoenix import _bars_to_dicts

    rows, censor = _bars_to_dicts(
        pl.DataFrame([{"date": date(2026, 1, 1), "raw_close": 1.0}]),
        "600000.SH",
    )
    assert rows == []
    assert censor["code"] == "raw_field_missing"


def test_trading_semantics_in_evidence_are_rejected():
    from app.services.n_shape_golden_phoenix import assert_no_trading_tokens

    with pytest.raises(ValueError):
        assert_no_trading_tokens("target_price")
class _FakePitProvider:
    def provider_id(self) -> str:
        return "pit-test"

    def limit_up_pct(self, symbol: str, on_date: date) -> float | None:
        return 0.10


class _FakePinnedReader:
    def __init__(self, bars: list[dict], manifest: str = "a" * 64):
        self._bars = bars
        self._manifest = manifest

    def generation(self) -> str:
        return "generation-test"

    def manifest_sha256(self) -> str:
        return self._manifest

    def market_days(self, start: date, end: date) -> list[date]:
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        rows = [row for row in self._bars if start <= row["date"] <= end]
        return pl.DataFrame(rows)


class _FakeRepo:
    def __init__(self, reader):
        self.generation_pinned_daily_reader = reader


def _bar(day: date, close: float, high: float, low: float, volume: float) -> dict:
    return {
        "date": day,
        "raw_close": close,
        "raw_high": high,
        "raw_low": low,
        "volume": volume,
        "close": close,
    }


def _base_bars(first_board: date) -> list[dict]:
    bars = [
        _bar(first_board - timedelta(days=i), 9.9, 10.5, 9.8, 1000.0)
        for i in range(70, 0, -1)
    ]
    bars.append(_bar(first_board, 10.89, 10.89, 10.5, 2000.0))
    return bars


def _evaluate(bars: list[dict], first_board: date, end_offset: int = 12):
    return evaluate_n_shape(
        start=first_board,
        end=first_board + timedelta(days=end_offset),
        symbols=["600000.SH"],
        pinned_reader=_FakePinnedReader(bars),
        pit_provider=_FakePitProvider(),
    )


def _ordinary_after(first_board: date, *, t1_volume: float = 800.0) -> list[dict]:
    bars = [
        _bar(first_board + timedelta(days=1), 10.7, 10.8, 10.6, t1_volume),
        _bar(first_board + timedelta(days=2), 11.77, 11.77, 10.6, 500.0),
    ]
    bars.extend(
        _bar(first_board + timedelta(days=i), 10.8, 11.0, 10.6, 900.0)
        for i in range(3, 13)
    )
    return bars


def test_event_evidence_uses_non_trading_range_key():
    first_board = date(2026, 1, 1)
    result = _evaluate(_base_bars(first_board) + _ordinary_after(first_board), first_board)
    assert result["status"] == "ok"
    event = next(item for item in result["events"] if item["variant"] == "second_limit_up")
    fields = {item["field"] for item in event["evidence"]}
    assert "price_range_rank_60d" in fields
    assert all("position" not in field for field in fields)


def test_confirmation_day_low_break_is_censored():
    first_board = date(2026, 1, 1)
    after = _ordinary_after(first_board)
    after[1]["raw_low"] = 10.4
    result = _evaluate(_base_bars(first_board) + after, first_board)
    assert result["events"] == []
    assert any(item["code"] == "structural_break_raw_low" for item in result["censored"])


def test_shrink_gate_excludes_confirmation_bar():
    first_board = date(2026, 1, 1)
    result = _evaluate(
        _base_bars(first_board) + _ordinary_after(first_board, t1_volume=1600.0),
        first_board,
    )
    assert result["events"] == []
    assert result["censored"] == []


def test_truncated_post_window_is_censored():
    first_board = date(2026, 1, 1)
    result = _evaluate(
        _base_bars(first_board) + _ordinary_after(first_board)[:3],
        first_board,
        end_offset=3,
    )
    truncated = [item for item in result["censored"] if item["code"] == "post_window_truncated"]
    assert len(truncated) == 1
    assert truncated[0]["detail"]["window_days_expected"] == 10
    assert truncated[0]["detail"]["window_days_available"] == 3


def test_ma_evidence_records_the_average_that_passed():
    first_board = date(2026, 1, 1)
    bars = _base_bars(first_board)
    bars.extend([
        _bar(first_board + timedelta(days=1), 11.8, 11.9, 10.6, 800.0),
        _bar(first_board + timedelta(days=2), 11.7, 11.8, 10.6, 800.0),
        _bar(first_board + timedelta(days=3), 11.0, 11.1, 10.7, 1500.0),
    ])
    bars.extend(
        _bar(first_board + timedelta(days=i), 10.8, 11.0, 10.6, 900.0)
        for i in range(4, 13)
    )
    result = _evaluate(bars, first_board)
    event = next(item for item in result["events"] if item["variant"] == "volume_breakout")
    evidence = {item["field"]: item for item in event["evidence"]}
    assert evidence["confirm_ma_basis"]["actual"] == "ma10"
    assert evidence["ma10_raw_close"]["actual"] == pytest.approx(10.479, abs=1e-4)
    assert "ma5_raw_close" not in evidence


def test_manifest_and_parameter_provenance_are_recorded():
    first_board = date(2026, 1, 1)
    result = _evaluate(_base_bars(first_board) + _ordinary_after(first_board), first_board)
    provenance = result["provenance"]
    assert provenance["pinned_reader"]["manifest_sha256"] == "a" * 64
    assert provenance["factor_code"]["factor_id"] == "n_shape_golden_phoenix_v1"
    params = provenance["factor_code"]["params"]
    assert params["prior_clean_days"] == 60
    assert params["price_range_rank_60d_max"] == 0.35
    assert params["post_window_min"] == 2
    assert params["post_window_max"] == 10
    assert params["volume_shrink_ratio"] == 0.7
    assert params["volume_pre20_ratio"] == 0.9
    assert params["volume_breakout_ratio"] == 1.5
    assert params["ma_window"] == 5
    assert params["limit_price_tol"] == 0.005
    assert params["forward_horizons"] == [1, 5, 10, 20]


def test_manifest_identity_is_required_and_validated():
    first_board = date(2026, 1, 1)
    bars = _base_bars(first_board) + _ordinary_after(first_board)

    class _NoManifestReader(_FakePinnedReader):
        manifest_sha256 = None

    assert resolve_pinned_reader(_FakeRepo(_NoManifestReader(bars))) is None
    invalid = evaluate_n_shape(
        start=first_board,
        end=first_board + timedelta(days=12),
        symbols=["600000.SH"],
        pinned_reader=_FakePinnedReader(bars, manifest="not-a-sha256"),
        pit_provider=_FakePitProvider(),
    )
    assert invalid["status"] == "unavailable"
    assert invalid["unavailable_reasons"] == ["reader_manifest_identity_invalid"]
