"""文件日志统一落盘: data/logs/ 子目录 + 大小切割 + 旧位置一次性迁移。

main.py (backend.log, 源码/服务器模式) 与 desktop.py:_setup_logging
(desktop.log, 桌面版) 共用。历史上两个文件直接写在 data/ 根下与业务数据
混放, 且桌面版用普通 FileHandler 追加、不切割 —— 长期挂机的 desktop.log
会无限增长 (用户反馈的磁盘隐患)。
"""
import contextlib
import logging
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)

# 迁移旧位置时最多回看的轮转编号 (RotatingFileHandler 现存产物 .1/.2/...)
_LEGACY_ROTATIONS = 5

# 滚动失败容忍次数: 首次失败只告警 (占用可能是瞬时的, 下条日志会自动重试);
# 连续第二次失败即停用文件输出 — logging 默认 handleError 会把完整堆栈逐条
# 日志打到 stderr, 用户实测 Windows 下终端被 PermissionError [WinError 32]
# 循环刷屏且期间文件日志全部丢失。
_MAX_ROTATE_FAILURES = 2


class StallSafeRotatingFileHandler(RotatingFileHandler):
    """Windows 友好的轮转 handler — 滚动被文件占用卡死时逐级降级。

    标准 doRollover 最终用 os.rename 滚动主日志, Windows 上该文件只要被
    任何进程握着句柄 (残留 backend 进程 / 编辑器打开 / 杀毒扫描) 就抛
    WinError 32。两级降级:
    1. rotate: rename 失败 → copy-and-truncate (内容复制到轮转文件后清空
       原文件 — 清自己持有的句柄不需要 DELETE 访问权, 不受他人读句柄影响);
    2. handleError: 降级后仍连续失败 → 打一条带指引的 ERROR 并把自己从
       root 摘除关闭; 终端 StreamHandler 不受影响, 不再逐条刷堆栈。
    """

    _failures = 0
    _stopping = False

    def rotate(self, source, dest) -> None:
        try:
            os.rename(source, dest)
        except OSError:
            with open(source, "rb") as fsrc, open(dest, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
            with open(source, "r+b") as f:
                f.truncate(0)
        self._failures = 0

    def handleError(self, record) -> None:  # noqa: N802 — 覆写 stdlib 同名钩子
        # 停用流程中自身的 ERROR 再触发失败 → 直接吞, 防 handleError 递归
        if self._stopping:
            return
        self._failures += 1
        exc = sys.exc_info()[1]
        if self._failures < _MAX_ROTATE_FAILURES:
            logger.warning("文件日志滚动失败 (%s): %s — 若持续失败将停用文件输出",
                           type(exc).__name__ if exc else "未知错误", exc)
            return
        self._stopping = True
        logger.error(
            "文件日志已停用: %s。多为日志文件被其他进程占用 (Windows: 残留 backend 进程"
            "或编辑器打开 %s)。终端日志不受影响, 清理占用后重启恢复。",
            exc, self.baseFilename,
        )
        root = logging.getLogger()
        if self in root.handlers:
            root.removeHandler(self)
        with contextlib.suppress(Exception):  # 关闭失败不值得再产生新噪音
            self.close()


def _migrate_legacy(log_dir, filename: str) -> None:
    """把 data/ 根下的旧日志 (含轮转副本 .1..N) 尽力搬进 logs/ 子目录。

    目标已存在或搬迁失败 (Windows 下被占用等) 一律跳过: 旧文件留在原地
    无害, 新日志在 logs/ 重新开始, 绝不为搬文件冒启动风险。
    """
    for name in [filename] + [f"{filename}.{i}" for i in range(1, _LEGACY_ROTATIONS + 1)]:
        src = log_dir.parent / name
        if not src.is_file():
            continue
        dst = log_dir / name
        if dst.exists():
            continue
        try:
            os.replace(src, dst)
        except OSError:
            pass


def attach_rotating_log(filename: str, *, max_mb: int, backup_count: int) -> None:
    """给 root logger 追加一个写 data/logs/<filename> 的轮转文件 handler。

    任何失败只 warning 不抛 —— 日志落盘永远不阻断启动
    (对齐 main.py / desktop.py 两处调用点原有的容错语义)。
    """
    try:
        from app.config import settings

        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _migrate_legacy(log_dir, filename)
        handler = StallSafeRotatingFileHandler(
            log_dir / filename,
            maxBytes=max_mb * 1024 * 1024,
            backupCount=backup_count,
            mode="a", encoding="utf-8", errors="replace",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(handler)
    except Exception as e:  # noqa: BLE001
        logger.warning("文件日志初始化失败, 仅输出到终端: %s", e)
