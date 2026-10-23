"""文件日志统一落盘: data/logs/ 子目录 + 大小切割 + 旧位置一次性迁移。

main.py (backend.log, 源码/服务器模式) 与 desktop.py:_setup_logging
(desktop.log, 桌面版) 共用。历史上两个文件直接写在 data/ 根下与业务数据
混放, 且桌面版用普通 FileHandler 追加、不切割 —— 长期挂机的 desktop.log
会无限增长 (用户反馈的磁盘隐患)。
"""
import logging
import os
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)

# 迁移旧位置时最多回看的轮转编号 (RotatingFileHandler 现存产物 .1/.2/...)
_LEGACY_ROTATIONS = 5


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
        handler = RotatingFileHandler(
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
