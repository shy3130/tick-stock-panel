from __future__ import annotations


def _candidate(
    symbol: str,
    *,
    name: str = "测试股票",
    decision: str = "GO",
    score: float = 80.0,
    close: float = 10.0,
    risk_flags: list[dict] | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "decision": decision,
        "decision_label": {
            "GO": "可进入研究清单",
            "WAIT": "等待更多确认",
            "NO-GO": "暂不纳入",
        }[decision],
        "score": score,
        "close": close,
        "strategies": ["alpha", "beta"],
        "strategy_count": 2,
        "risk_flags": risk_flags or [],
    }


def test_beginner_progress_filters_before_top_three_and_promotes_a_hidden_ready_stock():
    from app.services.advisor import build_beginner_candidate_progress

    current = [
        _candidate("300001.SZ", score=95.0),
        _candidate("688001.SH", score=94.0),
        _candidate("920001.BJ", score=93.0),
        _candidate("000759.SZ", name="中百集团", score=80.0, close=6.93),
    ]
    history = [
        {
            "as_of": "2026-08-04",
            "candidates": [
                _candidate("000759.SZ", name="中百集团", score=77.2, close=6.91),
            ],
        }
    ]

    result = build_beginner_candidate_progress(
        current,
        as_of="2026-08-05",
        history=history,
        trading_dates=["2026-08-04", "2026-08-05"],
        practice_capital=10_000,
    )

    assert [item["symbol"] for item in result["candidates"]] == ["000759.SZ"]
    [candidate] = result["candidates"]
    assert candidate["candidate_state"] == "READY"
    assert candidate["go_streak"] == 2
    assert candidate["global_rank"] == 4
    assert candidate["lot_size"] == 100
    assert candidate["lot_cost"] == 693.0


def test_beginner_progress_treats_friday_and_monday_as_adjacent_trading_days():
    from app.services.advisor import build_beginner_candidate_progress

    result = build_beginner_candidate_progress(
        [_candidate("003032.SZ", name="传智教育", close=12.03)],
        as_of="2026-08-10",
        history=[
            {
                "as_of": "2026-08-07",
                "candidates": [
                    _candidate("003032.SZ", name="传智教育", close=11.5),
                ],
            }
        ],
        trading_dates=["2026-08-07", "2026-08-10"],
        practice_capital=10_000,
    )

    assert result["candidates"][0]["candidate_state"] == "READY"
    assert result["candidates"][0]["previous_as_of"] == "2026-08-07"


def test_beginner_progress_does_not_bridge_a_missing_published_trading_day():
    from app.services.advisor import build_beginner_candidate_progress

    result = build_beginner_candidate_progress(
        [_candidate("600000.SH")],
        as_of="2026-08-05",
        history=[
            {
                "as_of": "2026-08-03",
                "candidates": [_candidate("600000.SH")],
            }
        ],
        trading_dates=["2026-08-03", "2026-08-04", "2026-08-05"],
        practice_capital=10_000,
    )

    assert result["candidates"][0]["candidate_state"] == "GO1"
    assert result["candidates"][0]["go_streak"] == 1
    assert result["candidates"][0]["previous_as_of"] == "2026-08-04"
    assert result["candidates"][0]["previous_decision"] is None


def test_beginner_progress_excludes_st_non_main_board_risky_and_over_budget_rows():
    from app.services.advisor import build_beginner_candidate_progress

    result = build_beginner_candidate_progress(
        [
            _candidate("600001.SH", name="*ST测试"),
            _candidate("300001.SZ"),
            _candidate(
                "600002.SH",
                risk_flags=[{"code": "LIMIT_UP", "message": "涨停"}],
            ),
            _candidate("600003.SH", close=31.0),
            _candidate("600004.SH", close=20.0),
        ],
        as_of="2026-08-05",
        history=[],
        trading_dates=["2026-08-05"],
        practice_capital=10_000,
    )

    assert [item["symbol"] for item in result["candidates"]] == ["600004.SH"]
    assert result["candidates"][0]["candidate_state"] == "GO1"
    assert result["excluded_counts"] == {
        "not_main_board": 1,
        "st_or_risk_warning": 1,
        "hard_risk": 1,
        "over_practice_budget": 1,
    }


def test_beginner_progress_warns_after_ten_complete_days_without_ready_candidate():
    from app.services.advisor import build_beginner_candidate_progress

    dates = [f"2026-08-{day:02d}" for day in range(1, 11)]
    history = [
        {
            "as_of": as_of,
            "candidates": [_candidate(f"6000{index:02d}.SH")],
        }
        for index, as_of in enumerate(dates[:-1])
    ]
    result = build_beginner_candidate_progress(
        [_candidate("601999.SH")],
        as_of=dates[-1],
        history=history,
        trading_dates=dates,
        practice_capital=10_000,
    )

    assert result["model_health"]["status"] == "WARNING"
    assert result["model_health"]["sample_days"] == 10
    assert "10" in result["model_health"]["message"]


def test_beginner_progress_reports_published_snapshot_count_not_calendar_window_size():
    from app.services.advisor import build_beginner_candidate_progress

    dates = [f"2026-08-{day:02d}" for day in range(1, 11)]
    history = [
        {"as_of": as_of, "candidates": []}
        for as_of in dates[1:8]
    ]

    result = build_beginner_candidate_progress(
        [],
        as_of=dates[-1],
        history=history,
        trading_dates=dates,
        practice_capital=10_000,
    )

    assert result["model_health"]["status"] == "INSUFFICIENT_HISTORY"
    assert result["model_health"]["sample_days"] == 8
    assert "8" in result["model_health"]["message"]
