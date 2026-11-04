"""启动阶段计时，定位就绪前没有其他日志的阻塞调用。"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def startup_phase(logger: logging.Logger, name: str) -> Iterator[None]:
    started = time.perf_counter()
    logger.info("startup phase start: %s", name)
    try:
        yield
    finally:
        # end 只表示调用结束；异常继续由原调用链处理，不把失败当成成功。
        logger.info("startup phase end: %s (%.3fs)", name, time.perf_counter() - started)
