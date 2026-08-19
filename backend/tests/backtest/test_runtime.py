from datetime import UTC, datetime, timedelta

from app.backtest.runtime import build_runtime, elapsed_ms_since, format_params


def test_format_params_sorted():
    assert format_params({"slow": 20, "fast": 5}) == "fast=5 · slow=20"
    assert format_params({}) == ""


def test_build_runtime_eta_from_elapsed(monkeypatch):
    started = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "app.backtest.runtime.datetime",
        type("D", (), {"now": staticmethod(lambda tz=None: started + timedelta(seconds=20)), "fromisoformat": datetime.fromisoformat}),
    )
    payload = build_runtime(
        stage="train",
        label="训练评估",
        current="ma · 全A · 5日",
        completed=2,
        total=10,
        failed=1,
        ok=1,
        started_at=started.isoformat(),
        last_elapsed_ms=9000,
    )
    assert payload["stage"] == "train"
    assert payload["failed"] == 1
    assert payload["elapsed_ms"] == 20_000
    assert payload["eta_ms"] == 80_000
    assert elapsed_ms_since(started.isoformat()) >= 0
