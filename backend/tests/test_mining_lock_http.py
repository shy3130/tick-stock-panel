"""Opening the workbench must leave the real HTTP backend alive on Windows too."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import httpx
import psutil
import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_SERVER_SCRIPT = """
import json, os, socket, sys
from pathlib import Path
from types import SimpleNamespace
import uvicorn
from fastapi import FastAPI
from app.api.mining import router
from app.services import mining_lock_admin
from app.services.mining_process_lock import MiningProcessLock

data_dir = Path(sys.argv[1])
lock = MiningProcessLock(data_dir)
lock.acquire()
if sys.argv[2] == "unreadable":
    mining_lock_admin.read_lock_holder = lambda _: None
app = FastAPI()
app.include_router(router)
app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
app.state.mining_process_lock = lock
app.state.mining_lock_acquired = True

@app.get("/__test/health")
def health():
    return {"pid": os.getpid(), "owns_lock": lock.acquired}

server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))

@app.post("/__test/stop")
def stop():
    server.should_exit = True

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    (data_dir / "server.json").write_text(json.dumps({
        "pid": os.getpid(), "port": sock.getsockname()[1],
    }), encoding="utf-8")
    try:
        server.run(sockets=[sock])
    finally:
        lock.release()
"""


@pytest.mark.parametrize("metadata", ["readable", "unreadable"])
def test_workbench_status_polling_keeps_http_backend_alive(tmp_path, metadata):
    run_dir = tmp_path / "research" / "mining" / "runs" / "live"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "manifest.json"
    original = b'{"status": "running"}'
    manifest.write_bytes(original)
    server_info = tmp_path / "server.json"
    output_path = tmp_path / "server.log"
    with output_path.open("w", encoding="utf-8") as output:
        proc = subprocess.Popen(
            [sys.executable, "-c", _SERVER_SCRIPT, str(tmp_path), metadata],
            cwd=_BACKEND_ROOT,
            env={**os.environ, "PYTHONPATH": str(_BACKEND_ROOT), "PYTHONUTF8": "1"},
            stdout=output, stderr=subprocess.STDOUT,
            # A regressed console signal must not reach the pytest runner's console.
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
        )
        try:
            with httpx.Client(timeout=2, trust_env=False) as client:
                deadline = time.monotonic() + 20
                while True:
                    assert proc.poll() is None, output_path.read_text(encoding="utf-8")
                    if server_info.exists():
                        try:
                            info = json.loads(server_info.read_text(encoding="utf-8"))
                            base = f"http://127.0.0.1:{info['port']}"
                            if client.get(f"{base}/__test/health").status_code == 200:
                                break
                        except (json.JSONDecodeError, httpx.TransportError):
                            pass
                    assert time.monotonic() < deadline, output_path.read_text(encoding="utf-8")
                    time.sleep(0.05)
                # Windows' venv launcher PID may differ from the actual server PID.
                server_process = psutil.Process(info["pid"])
                for _ in range(10):
                    response = client.get(f"{base}/api/backtest/mining/lock")
                    assert response.status_code == 200, response.text
                    status = response.json()
                    assert status["mine"] is True
                    assert status["holder_alive"] is True
                    assert status["holder"]["pid"] == info["pid"]
                    assert status["active_runs"] == 1
                    health = client.get(f"{base}/__test/health")
                    assert health.status_code == 200
                    assert health.json() == {"pid": info["pid"], "owns_lock": True}
                assert server_process.is_running()
                assert manifest.read_bytes() == original
                assert client.post(f"{base}/__test/stop").status_code == 200
                assert proc.wait(timeout=10) == 0
        finally:
            if proc.poll() is None:
                # Clean up only this fixture's process tree, including venv launchers.
                with suppress(psutil.NoSuchProcess):
                    for child in psutil.Process(proc.pid).children(recursive=True):
                        with suppress(psutil.NoSuchProcess):
                            child.kill()
                proc.kill()
                proc.wait(timeout=10)
