from contextlib import contextmanager
from pathlib import Path

import pytest

from app.services.full_market_research import FullMarketRunnerError
from scripts import run_full_market_research as cli


def test_runner_forces_conservative_native_thread_limits():
    assert all(1 <= int(cli.os.environ[name]) <= 2 for name in cli.THREAD_ENV_VARS)


def test_runner_uses_single_instance_eight_gib_default():
    assert cli.DEFAULT_MAX_RSS_GIB == 8.0


def test_runner_lock_rejects_concurrent_process(tmp_path: Path):
    lock_path = tmp_path / "full-market.lock"

    with (
        cli._single_run_lock(lock_path),
        pytest.raises(FullMarketRunnerError, match="another full-market research"),
        cli._single_run_lock(lock_path),
    ):
        pass


def test_rss_guard_validates_limit_without_starting_unbounded_run():
    assert cli._peak_rss_bytes() > 0
    with pytest.raises(ValueError, match="max-rss-gib"), cli._rss_guard(0):
        pass


def test_main_keeps_guards_active_through_output(monkeypatch):
    active: set[str] = set()
    events: list[str] = []
    rss_limits: list[float] = []

    @contextmanager
    def fake_lock(_path):
        active.add("lock")
        try:
            yield
        finally:
            active.remove("lock")

    @contextmanager
    def fake_rss_guard(limit):
        rss_limits.append(limit)
        active.add("rss")
        try:
            yield
        finally:
            active.remove("rss")

    def fake_run(*_args, **_kwargs):
        assert active == {"lock", "rss"}
        events.append("run")
        return {"research_id": "test-research"}

    def fake_emit(_payload, _output):
        assert active == {"lock", "rss"}
        events.append("emit")

    monkeypatch.setattr(cli, "_single_run_lock", fake_lock)
    monkeypatch.setattr(cli, "_rss_guard", fake_rss_guard)
    monkeypatch.setattr(cli, "_build_repo", lambda _path: object())
    monkeypatch.setattr(cli, "run_full_market_research", fake_run)
    monkeypatch.setattr(cli, "_emit_payload", fake_emit)
    monkeypatch.setattr(cli, "full_market_factor_ids", lambda: ("macd-arms",))

    assert (
        cli.main(
            [
                "--factor",
                "macd-arms",
                "--start",
                "2024-01-01",
                "--end",
                "2024-12-31",
            ]
        )
        == 0
    )
    assert events == ["run", "emit"]
    assert rss_limits == [cli.DEFAULT_MAX_RSS_GIB]
