"""TdxQuant 候选 Key 探测 — 子进程拉起方式与 worker 行为。

回归背景: 打包版 sys.executable 是应用本身, `-c` 被 PyInstaller bootloader
无视 → 探测子进程重启整个桌面应用而非执行探测 (Key 永远验证失败)。
拉起方式因此分叉: dev 用 `python -c`, frozen 用「裸重执行自身 + 哨兵」
(desktop.main 最先分发到 run_worker), 两种入口共用同一份 worker 实现。
"""

import json
import os
import sys

import app.desktop as desktop
from app.plugins.tdxquant import probe


class _FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _capture_spawn(monkeypatch, frozen: bool) -> dict:
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["env"] = dict(kwargs["env"])
        return _FakeProc(stdout=json.dumps({"ok": True, "reason": ""}))

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)
    return seen


def test_run_dev_spawns_python_dash_c(monkeypatch):
    seen = _capture_spawn(monkeypatch, frozen=False)
    ok, _ = probe.run("TDX-KEY")
    assert ok
    assert seen["cmd"][:2] == [sys.executable, "-c"]
    assert "run_worker" in seen["cmd"][2]
    # dev 路径不设哨兵 — 那是 frozen 重执行自身的分发机制
    assert probe._PROBE_ENV_WORKER not in seen["env"]


def test_run_frozen_reexecs_self_with_sentinel(monkeypatch):
    seen = _capture_spawn(monkeypatch, frozen=True)
    ok, _ = probe.run("TDX-KEY")
    assert ok
    # 不带 -c: bootloader 会无视它并重启整个桌面应用
    assert seen["cmd"] == [sys.executable]
    assert seen["env"][probe._PROBE_ENV_WORKER] == "1"
    assert seen["env"][probe._PROBE_ENV_KEY] == "TDX-KEY"


def _patch_worker_env(monkeypatch, tmp_path):
    monkeypatch.setenv(probe._PROBE_ENV_KEY, "TDX-KEY")
    monkeypatch.setenv(probe._PROBE_ENV_TMP, str(tmp_path))
    monkeypatch.setenv(probe._PROBE_ENV_WORKER, "1")
    monkeypatch.setattr(
        "app.plugins.tdxquant.client.materialize_lib",
        lambda key, tmp, **kw: tmp_path / "fake_lib.so",
    )
    monkeypatch.setattr(probe.time, "sleep", lambda s: None)


def test_run_worker_unreachable_reports_json(monkeypatch, tmp_path, capsys):
    _patch_worker_env(monkeypatch, tmp_path)

    class _FakeNative:
        def get_market_snapshot_batch(self, *a, **kw):
            raise RuntimeError("down")

    import tdxaidata.tdxaidata as tdxmod

    monkeypatch.setattr(
        tdxmod._NativeTdxAiData, "get_instance", classmethod(lambda cls: _FakeNative())
    )

    assert probe.run_worker() == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "连接失败" in payload["reason"]
    progress = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert progress["stage"] == "done" and progress["ok"] is False


def test_run_worker_ok_path(monkeypatch, tmp_path, capsys):
    _patch_worker_env(monkeypatch, tmp_path)

    class _FakeNative:
        def get_market_snapshot_batch(self, *a, **kw):
            return {"price": 1.0}

        def get_his_dats(self, *a, **kw):
            return "raw"

    import tdxaidata.tdxaidata as tdxmod
    from tdxaidata import tqs

    monkeypatch.setattr(
        tdxmod._NativeTdxAiData, "get_instance", classmethod(lambda cls: _FakeNative())
    )
    monkeypatch.setattr(tqs, "_clean", lambda raw: {"Date": ["2026-01-02"]})

    assert probe.run_worker() == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["aux"] == "his_ok"


def test_run_worker_exception_reports_stage(monkeypatch, tmp_path, capsys):
    _patch_worker_env(monkeypatch, tmp_path)

    def _boom(key, tmp, **kw):
        raise RuntimeError("materialize failed")

    monkeypatch.setattr("app.plugins.tdxquant.client.materialize_lib", _boom)

    assert probe.run_worker() == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "materialize failed" in payload["reason"]
    progress = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert progress["stage"] == "exception"


def test_desktop_main_dispatches_probe_worker(monkeypatch):
    monkeypatch.setenv("TDX_PROBE_WORKER", "1")
    monkeypatch.setattr(desktop, "_guard_streams", lambda: None)
    monkeypatch.setattr(desktop, "_setup_logging", lambda: None)
    monkeypatch.setattr("app.plugins.tdxquant.probe.run_worker", lambda: 7)

    def _boom(*a, **kw):
        raise AssertionError("worker 模式不应触碰桌面启动流程")

    monkeypatch.setattr(desktop, "_ensure_data_dir_writable", _boom)
    monkeypatch.setattr(desktop, "_acquire_single_instance", _boom)
    assert desktop.main() == 7


def test_desktop_main_without_sentinel_starts_normally(monkeypatch):
    monkeypatch.delenv("TDX_PROBE_WORKER", raising=False)
    monkeypatch.setattr(desktop, "_guard_streams", lambda: None)
    monkeypatch.setattr(desktop, "_setup_logging", lambda: None)
    monkeypatch.setattr(desktop, "_ensure_data_dir_writable", lambda: None)
    # 单实例未抢到 → 正常退出, 不触碰后端/窗口
    monkeypatch.setattr(desktop, "_acquire_single_instance", lambda: False)
    assert desktop.main() == 0


def test_pid_alive_posix_current_process():
    assert desktop._pid_alive(os.getpid()) is True
