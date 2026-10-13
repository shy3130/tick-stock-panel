"""候选 Key 探测 — 独立子进程「格式 + 连通性」探测。

原生 DLL 每进程加载一次且绑定一个 token: 主进程可能已用旧 Key 加载,
候选 Key 无法在进程内替换, 因此探测必须另起进程(临时 lib 目录 +
TDX_AI_DATA_LIB 指向), 结论以 JSON 回传。

可靠性设计(实测教训):
  - DLL 原生握手固定 ~30s(同 IP 并发会话疑似服务端节流), 超时必须 >45s;
  - 子进程可能在原生层直接死亡(段错误级, 无任何输出) — 每阶段把进度写入
    父进程指定的进度文件, 父进程据此定位死点并附退出码, 不再返回"无输出";
  - TDX 会话按 IP 复用: 同 IP 有活跃好 Key 会话时(常驻后端必然如此),
    错误 Key 的日K也能返回 ⇒ "无效 Key 拒绝"不可靠。本探测只验证
    格式 + 连通性, Key 真伪由首次实拉(test_dataset / 日常轮询)暴露。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_PROBE_ENV_KEY = "TDX_PROBE_CANDIDATE_KEY"
_PROBE_ENV_TMP = "TDX_PROBE_TMP_DIR"
# frozen 版哨兵: 桌面版没有可用的 python 解释器 (sys.executable 是应用本身,
# -c 会被 PyInstaller bootloader 无视而重启整个桌面应用), 因此以
# 「重执行自身 + 本哨兵」拉起 worker, 由 desktop.main 最先分发到 run_worker
_PROBE_ENV_WORKER = "TDX_PROBE_WORKER"

# dev 模式子进程入口: 直接调用本模块的 run_worker, 与 frozen 哨兵共用同一份实现
_SCRIPT = "from app.plugins.tdxquant.probe import run_worker; run_worker()"


def run_worker() -> int:
    """探测 worker — 必须在独立子进程内执行 (见 run 的拉起方式)。

    从环境变量读候选 Key 与进度目录, 各阶段进度写入 progress.json,
    结论以单行 JSON 打到 stdout 供父进程解析。复用 client.materialize_lib
    保持与主进程同一套物化/写 token 逻辑。
    """
    key = os.environ.pop(_PROBE_ENV_KEY, "")
    tmp = Path(os.environ.pop(_PROBE_ENV_TMP, ""))
    os.environ.pop(_PROBE_ENV_WORKER, None)
    progress = tmp / "progress.json" if str(tmp) else None

    def stage(name: str, **extra) -> None:
        if progress is None:
            return
        try:
            progress.write_text(json.dumps({"stage": name, **extra}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    try:
        stage("imports")
        from app.plugins.tdxquant import client as tc

        stage("materialize")
        dll = tc.materialize_lib(key, tmp)
        os.environ[tc.LIB_ENV] = str(dll)

        stage("load_dll")
        from tdxaidata.tdxaidata import _NativeTdxAiData

        native = _NativeTdxAiData.get_instance()

        # 连通性判定: 快照端点不依赖 token 鉴权, 拿到数据即证明
        # 服务器可达 + DLL 正常。轮询容忍 DLL 冷启动的未就绪窗口。
        stage("start")
        reachable = False
        for _ in range(3):
            try:
                if native.get_market_snapshot_batch("600519.SH", "Now"):
                    reachable = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.2)
        if not reachable:
            stage("done", ok=False)
            print(json.dumps({"ok": False, "reason": "行情服务器连接失败(网络不可达)"}))
            return 0

        # 日K仅作辅助信号(会话按 IP 复用时错误 Key 也能通过, 不能作拒绝依据)
        stage("auth")
        aux = ""
        try:
            raw = native.get_his_dats(
                "600519.SH", "", "2026-01-01 00:00:00", "1d", "none", 3
            )
            from tdxaidata import tqs

            v = tqs._clean(raw)
            aux = "his_ok" if isinstance(v, dict) and v.get("Date") else "his_empty"
        except Exception:  # noqa: BLE001
            aux = "his_exc"

        stage("done", ok=True)
        print(json.dumps({"ok": True, "reason": "", "aux": aux}))
    except Exception as exc:  # noqa: BLE001
        stage("exception", reason=str(exc)[:300])
        print(json.dumps({"ok": False, "reason": str(exc)[:300]}))
    return 0


def _read_stage(tmp_dir: str) -> str:
    try:
        data = json.loads((Path(tmp_dir) / "progress.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 进度文件缺失/损坏按未知处理
        return "未知(无进度文件)"
    stage = str(data.get("stage", "?"))
    if stage == "exception":
        return f"异常: {str(data.get('reason', ''))[:150]}"
    return stage


def _tail(text: str, limit: int = 150) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1][:limit] if lines else "(空)"


def run(api_key: str, timeout_s: float = 75.0) -> tuple[bool, str]:
    """子进程探测候选 Key: 格式由调用方把关, 这里验证连通性(快照可达)。"""
    if not api_key:
        return False, "Key 为空"
    backend_root = Path(__file__).resolve().parents[3]
    tmp_dir = tempfile.mkdtemp(prefix="tdx_probe_")
    env = {
        **os.environ,
        _PROBE_ENV_KEY: api_key,
        _PROBE_ENV_TMP: tmp_dir,
        "PYTHONPATH": str(backend_root),
    }
    if getattr(sys, "frozen", False):
        # 打包版 sys.executable 是应用本身, `-c` 会被 bootloader 无视并重启
        # 整个桌面应用 — 必须裸重执行自身, 由 desktop.main 的哨兵分发 worker
        cmd = [sys.executable]
        env[_PROBE_ENV_WORKER] = "1"
    else:
        cmd = [sys.executable, "-c", _SCRIPT]
    try:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=str(backend_root),
                env=env,
            )
        except subprocess.TimeoutExpired:
            stage = _read_stage(tmp_dir)
            return False, f"探测超时({timeout_s:.0f}s, 停在阶段[{stage}]) — 原生握手约需 30s, 请检查网络后重试"
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    break
                if payload.get("ok"):
                    return True, "ok(连通正常; Key 有效性以首次拉取为准)"
                return False, f"探测失败: {payload.get('reason', '未知原因')}"
        # 无 JSON 结论: 原生层直接死亡(无输出)或入口即崩 — 用进度文件定位死点
        stage = _read_stage(tmp_dir)
        return False, (
            f"探测失败: 子进程死在阶段[{stage}], exit={proc.returncode}, "
            f"stderr={_tail(proc.stderr)}"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
