"""桌面客户端入口 — uvicorn 后台服务 + pywebview 桌面窗口。

运行方式:
  开发模式: python -m app.desktop  (需 pip install pywebview)
  打包后:   双击可执行文件即可

职责:
  1. 单实例锁 — 已运行则聚焦已有窗口并退出
  2. 选可用端口 — 从 settings.port 起, 被占则递增
  3. 后台线程起 uvicorn (仅监听 127.0.0.1, 不暴露外网)
  4. 主线程起 pywebview 窗口渲染前端
  5. 窗口关闭 → 优雅停止 uvicorn → 进程退出

不含: 业务逻辑、配置持久化、监控告警 (全在 app.main 里)。
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from pathlib import Path
from app.log_redaction import install_secret_redaction_filter

logger = logging.getLogger(__name__)

_APP_NAME = "TickFlow 股票面板"
_BASE_PORT = 3018
_PORT_PROBE_RANGE = 50  # 从 3018 起最多试 50 个端口


def _ensure_data_dir_writable() -> None:
    """确保用户数据目录可写 (lifespan 会创建子目录, 这里只验证根目录)。

    data_dir 在 frozen 模式下指向用户目录 (见 config.py), 非可写会导致
    DuckDB 视图 / parquet 落盘全失败。提前失败胜过启动后乱报错。
    """
    from app.config import settings

    data_root = settings.data_dir
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        probe = data_root / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.error("数据目录不可写, 桌面版无法运行: %s (%s)", data_root, e)
        raise


def _acquire_single_instance() -> bool:
    """单实例锁。已运行返回 False (本进程应退出), 否则 True。

    用 data_dir/.desktop.lock 文件锁实现。跨进程, 文件存在即视为已运行
    (简单可靠; 不引入 msvcrt/fcntl 平台差异)。
    """
    from app.config import settings

    lock_path = settings.data_dir / ".desktop.lock"
    if lock_path.exists():
        # 软检测: 写入进程 PID, 若该 PID 已不存在则视为残留锁, 允许接管
        try:
            pid_str = lock_path.read_text(encoding="utf-8").strip()
            pid = int(pid_str) if pid_str.isdigit() else None
        except Exception:  # noqa: BLE001
            pid = None

        if pid is not None and _pid_alive(pid):
            logger.warning("检测到已有实例运行 (PID %d), 本进程退出", pid)
            return False
        # 残留锁: 清理后继续
        logger.info("清理残留单实例锁 (PID %s 已不存在)", pid)

    lock_path.write_text(str(_current_pid()), encoding="utf-8")
    return True


def _release_single_instance() -> None:
    from app.config import settings

    lock_path = settings.data_dir / ".desktop.lock"
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _pid_alive(pid: int) -> bool:
    """检查指定 PID 的进程是否存活。"""
    import os

    if os.name == "nt":
        # Windows: 0 表示存在, 其它是异常
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    else:
        try:
            os.kill(pid, 0)  # signal 0 = 探测存活, 不实际发信号
            return True
        except OSError:
            return False


def _current_pid() -> int:
    import os

    return os.getpid()


def _find_free_port(start: int, count: int = _PORT_PROBE_RANGE) -> int:
    """从 start 起找第一个可用端口。全部被占则返回 start (交给 uvicorn 报错)。"""
    for port in range(start, start + count):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def _run_uvmicorn(port: int, ready_event: threading.Event) -> None:
    """后台线程: 启动 uvicorn 服务。ready_event 在线程退出时置位 (通知主线程)。"""
    import uvicorn

    # 延迟 import app, 确保配置层已就绪 (frozen 检测在 config.py 导入时完成)
    from app.main import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",  # 仅本机, 不暴露外网 (桌面版无需远程访问)
        port=port,
        log_level="info",
        access_log=False,    # 桌面版不需要访问日志
        loop="auto",
    )
    server = uvicorn.Server(config)

    # 线程结束时通知主线程 (无论正常退出还是异常)
    def _signal_done(*exc):
        ready_event.set()
    server.config.callback_notify = None  # 不用 notify 机制

    try:
        server.run()
    finally:
        ready_event.set()


def _wait_for_server(port: int, timeout: float = 60.0) -> bool:
    """轮询 health 接口直到后端就绪或超时。

    比 monkey-patch uvicorn 内部方法更健壮, 不依赖版本内部实现。
    """
    import urllib.request
    import urllib.error

    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.5)
    return False


def _open_window(url: str) -> None:
    """主线程: 用 pywebview 打开桌面窗口。"""
    import webview  # type: ignore[import-not-found]

    window = webview.create_window(
        _APP_NAME,
        url,
        width=1440,
        height=900,
        min_size=(1024, 700),
        # 桌面版固定单窗口, 禁用外部浏览器跳转
        confirm_close=False,
    )
    # pywebview 会阻塞主线程直到窗口关闭
    webview.start(debug=False)


def main() -> int:
    """桌面客户端主入口。返回进程退出码。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    install_secret_redaction_filter()

    try:
        _ensure_data_dir_writable()
    except Exception:
        # 数据目录不可写是致命错误, 无法继续
        return 1

    # 单实例: 已运行则退出
    if not _acquire_single_instance():
        return 0

    try:
        port = _find_free_port(_BASE_PORT)
        logger.info("桌面版后端将监听 127.0.0.1:%d", port)

        # 后台线程起 uvicorn
        ready = threading.Event()
        server_thread = threading.Thread(
            target=_run_uvmicorn, args=(port, ready), daemon=True,
            name="uvicorn",
        )
        server_thread.start()

        # 轮询 health 接口等后端就绪 (含 lifespan 初始化, 最多 60s)
        if not _wait_for_server(port, timeout=60.0):
            logger.error("后端启动超时, 桌面版退出")
            _release_single_instance()
            return 1

        url = f"http://127.0.0.1:{port}"
        logger.info("打开桌面窗口: %s", url)
        _open_window(url)

        # 窗口关闭后, 进程退出 (daemon 线程会被回收)
        logger.info("窗口已关闭, 桌面版退出")
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        _release_single_instance()


if __name__ == "__main__":
    sys.exit(main())
