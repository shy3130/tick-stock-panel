"""通过既有路由注册与 lifespan 接入，不修改主应用或核心 API。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.screener import ScreenerService
from app.strategy import paper_watch

from .service import ExecutionService

logger = logging.getLogger(__name__)


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str | None = Field(default=None, max_length=120)
    candidate_id: str | None = Field(default=None, max_length=120)
    config: dict | None = None
    strategy_info: dict | None = None
    label: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_source(self):
        if sum(v is not None for v in (self.strategy_id, self.candidate_id, self.config)) != 1:
            raise ValueError("请选择一个策略、回测候选或回测配置来源")
        if len(json.dumps(self.model_dump(), allow_nan=False)) > 128 * 1024:
            raise ValueError("回测配置过大，请只导入配置和策略信息")
        return self


class BindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Source
    revision: str = Field(min_length=64, max_length=64)
    disable_legacy: bool = False


class EnabledRequest(BaseModel):
    enabled: bool


def build_router() -> APIRouter:
    service: ExecutionService | None = None

    def sync_watch():
        return None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal service, sync_watch
        thread = None
        lease = None
        try:
            repo = getattr(app.state, "repo", None)
            engine = getattr(app.state, "strategy_engine", None)
            if repo is None or engine is None:
                raise ValueError("策略引擎或数据仓库未就绪")
            data_dir = Path(repo.store.data_dir)
            root = data_dir / "user_data" / "extensions" / "paper_execution"
            root.mkdir(parents=True, exist_ok=True)
            # SQLite 的操作系统锁跨平台、随进程退出释放；禁止多个 worker 同时跟单。
            lease = sqlite3.connect(root / "runner.sqlite", timeout=0)
            lease.execute("BEGIN EXCLUSIVE")
            service = ExecutionService(
                data_dir, engine, ScreenerService(repo), getattr(app.state, "quote_service", None)
            )

            def refresh_watch():
                return paper_watch.sync_from_state(app.state, data_dir)

            sync_watch = refresh_watch
            active_service = service

            def run():
                while not active_service.stopping.is_set():
                    try:
                        active_service.tick()
                    except Exception:
                        logger.exception("paper execution worker failed")
                    active_service.stopping.wait(15)

            thread = threading.Thread(target=run, name="paper-execution", daemon=True)
            thread.start()
        except Exception:
            logger.exception("paper execution extension unavailable")
            service = None
        try:
            yield
        finally:
            if service:
                service.stopping.set()
            if thread:
                # 所有提交路径检查 stopping；慢策略计算结束后也不会再下单。
                thread.join(timeout=3)
            if lease:
                lease.close()
            service = None

    router = APIRouter(
        prefix="/api/custom/paper-execution", tags=["custom-paper-execution"], lifespan=lifespan
    )

    def require_service() -> ExecutionService:
        if service is None:
            raise HTTPException(503, "策略跟单扩展未就绪或已有其他执行进程，请检查后端日志")
        return service

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            # 不返回路径、数据配置或堆栈；可读领域错误只含业务说明。
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "扩展数据无法读取或保存，请检查数据目录和配置"
            )
            raise HTTPException(409, message) from exc

    @router.get("/bindings")
    def list_bindings():
        return {"bindings": call(require_service().list_bindings)}

    @router.get("/state")
    def get_state(account: str = "default"):
        return {"binding": call(require_service().get, account)}

    @router.post("/preview")
    def preview(body: Source, account: str = "default"):
        return call(require_service().preview, body.model_dump(exclude_none=True), account)

    @router.put("/binding")
    def bind(body: BindingRequest, account: str = "default"):
        binding = call(
            require_service().bind,
            account,
            body.source.model_dump(exclude_none=True),
            body.revision,
            disable_legacy=body.disable_legacy,
        )
        sync_watch()
        return {"binding": binding}

    @router.post("/enabled")
    def set_enabled(body: EnabledRequest, account: str = "default"):
        return {"binding": call(require_service().set_enabled, account, body.enabled)}

    return router
