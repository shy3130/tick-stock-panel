"""FastAPI 入口。"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import (
    abnormal,
    alerts,
    analysis,
    backtest,
    data,
    ext_data,
    factors,
    financials,
    indices,
    intraday,
    kline,
    lots,
    market_recap,
    mining,
    monitor_rules,
    overview,
    pipeline,
    regime,
    rps,
    screener,
    signals,
    stock_analysis,
    strategy,
    watchlist,
)
from app.api import auth as auth_api
from app.api import settings as settings_api
from app.api.routes import router as core_router
from app.config import settings
from app.enriched_generation import EnrichedGenerationUnavailableError
from app.extensions.loader import (
    configure_backend_extensions,
    current_extension_context,
    start_backend_extensions,
)
from app.jobs import daily_pipeline
from app.services.matrix_prewarm_owner import MatrixCachePrewarmOwner
from app.services.mining_process_lock import MiningProcessLock
from app.services.quote_service import QuoteService
from app.tickflow import client as tf_client
from app.tickflow.policy import detect_capabilities
from app.tickflow.repository import DataStore, KlineRepository

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 追加文件日志: uvicorn (含 --reload 开发模式) 默认只有 StreamHandler, 同步/管道等
# 运行时日志仅出现在 dev 终端, 关掉或滚屏后即丢失, 排查「同步后日志没落」时无处可查。
# 落盘到 data/backend.log 与桌面版 (desktop.py:_setup_logging → desktop.log) 行为对齐,
# 事后可查。桌面版 (frozen) 已由 desktop.py 写 desktop.log, 此处跳过避免重复落盘。
# RotatingFileHandler 防止长期运行/频繁 reload 导致文件无限增长。
if not getattr(sys, "frozen", False):
    try:
        from logging.handlers import RotatingFileHandler

        _log_path = settings.data_dir / "backend.log"
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        _file_handler = RotatingFileHandler(
            _log_path, maxBytes=10 * 1024 * 1024, backupCount=3,
            mode="a", encoding="utf-8", errors="replace",
        )
        _file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(_file_handler)
    except Exception as _e:  # noqa: BLE001
        logger.warning("文件日志初始化失败, 仅输出到终端: %s", _e)


@asynccontextmanager
async def _application_lifespan(app: FastAPI):
    logger.info(
        "Tick Stock Panel v%s starting (mode=%s)",
        __version__, tf_client.current_mode(),
    )

    # 首次启动: 若配置了 AUTH_PASSWORD 环境变量且未设过密码, 用它初始化。
    # 公网部署免 SSH 端口转发; 已设过密码则不覆盖 (改密码走 UI)。
    try:
        from app.services import auth as auth_service
        auth_service.bootstrap_from_env()
    except Exception as e:  # noqa: BLE001
        logger.warning("auth bootstrap failed: %s", e)

    # 数据层
    store = DataStore()
    repo = KlineRepository(store)
    app.state.datastore = store
    app.state.repo = repo
    # 自定义/复合因子载入注册表 (P3); 单个失败只跳过该因子 (fail-隔离)
    from app.factors.store import load_into_registry

    try:
        loaded_factors = load_into_registry(store.data_dir)
        if loaded_factors:
            logger.info("custom factors loaded: %s", len(loaded_factors))
    except Exception as exc:  # noqa: BLE001
        logger.warning("custom factors load failed: %s", exc)
    from app.services.mining_manager import MiningJobManager

    mining_manager = MiningJobManager(store.data_dir)
    recovered_mining_runs = mining_manager.recover_interrupted()
    app.state.mining_manager = mining_manager
    if recovered_mining_runs:
        logger.warning("recovered %d interrupted mining runs", recovered_mining_runs)
    # 在接受回测请求前固定 managed generation，避免首批并发 worker 各自创建版本。
    if settings.backtest_matrix_disk_cache_enabled:
        try:
            repo.get_matrix_data_generation("stock")
        except EnrichedGenerationUnavailableError as exc:
            logger.warning("enriched generation requires a full rebuild: %s", exc)
    # 指标异步预热标志: enriched 缓存在后台线程构建, 完成后置 True
    app.state.indicators_ready = False
    repo._on_warmup_done = lambda: setattr(app.state, "indicators_ready", True)  # noqa: SLF001

    # Polars 缓存预热 — enriched 的重计算 (107万行 compute_indicators) 推后台,
    # instruments/index/ETF 仍同步 (毫秒级)。应用立即 ready, 指标算完后自动替换。
    repo.refresh_cache(background=True)

    # 自定义数据源配置(可选): 失败只记录错误, 不影响 TickFlow 基准路径。
    try:
        from app.data_providers import custom as custom_sources
        custom_sources.load_all()
        logger.info("custom data sources loaded: %d", len(custom_sources.list_sources()))
    except Exception as e:  # noqa: BLE001
        logger.warning("custom data sources init failed: %s", e)

    # 自定义源必须先注册,能力探测才能补充其数据集能力。
    capset = detect_capabilities()
    app.state.capabilities = capset
    logger.info("ready; %d capabilities active", len(capset.all()))

    # 全局行情服务
    qs = QuoteService()
    app.state.quote_service = qs
    qs.set_repo(repo)
    qs.boot_check()

    # QuoteService 需要访问 strategy_monitor 等单例
    # 先创建 strategy_monitor，再注入 app.state
    from app.strategy.monitor import StrategyMonitorService
    strategy_monitor = StrategyMonitorService()
    app.state.strategy_monitor = strategy_monitor
    qs.set_app_state(app.state)

    # 五档盘口 sealed 服务(真假涨停/跌停, 独立旁路线)
    from app.services.depth_service import DepthService
    depth_service = DepthService()
    depth_service.set_repo(repo)
    depth_service.set_app_state(app.state)
    app.state.depth_service = depth_service

    # 启动调度器(若 enriched 数据为空,首次启动可手动 POST /api/pipeline/run)
    try:
        daily_pipeline.set_app_state(app.state)  # 供 depth_finalize job 访问 depth_service
        scheduler = daily_pipeline.start_scheduler(repo, capset)
        app.state.scheduler = scheduler
    except Exception as e:  # noqa: BLE001
        logger.warning("scheduler not started: %s", e)
        app.state.scheduler = None

    # depth sealed: 启动补跑(当天文件不存在) + 盘中轮询(有能力时)
    try:
        depth_service.boot_check()
        depth_service.start_polling()
    except Exception as e:  # noqa: BLE001
        logger.warning("depth_service init failed: %s", e)

    # 盘中分钟增量刷新 (Expert 专有): 线程常驻, 开关/时段/能力门控在循环内每轮判断
    try:
        from app.services.minute_refresh import MinuteRefreshService
        minute_refresh = MinuteRefreshService(repo)
        minute_refresh.set_app_state(app.state)
        app.state.minute_refresh = minute_refresh
        minute_refresh.start()
    except Exception as e:
        logger.warning("minute_refresh init failed: %s", e)

    # 停机缺口自检: 延迟后台扫描, 发现最近交易日的盘中快照/缺口时自动创建
    # 修复任务 (盘中停机→次日开实时场景, 不修则坏数据被"只刷今天"分支永久留存)
    try:
        import threading

        from app.services.data_integrity import boot_integrity_check

        timer = threading.Timer(30.0, boot_integrity_check, args=(app.state,))
        timer.daemon = True  # 不阻塞进程退出
        timer.start()
    except Exception as e:  # noqa: BLE001
        logger.warning("integrity boot check scheduling failed: %s", e)

    # 企业微信智能机器人长连接(可选通道, 失败不阻断启动)
    try:
        from app.services.wecom_bot_service import WecomBotService
        wecom_bot_service = WecomBotService()
        wecom_bot_service.set_app_state(app.state)
        app.state.wecom_bot_service = wecom_bot_service
        wecom_bot_service.boot_check()
    except Exception as e:  # noqa: BLE001
        logger.warning("wecom_bot_service init failed: %s", e)

    # 内置扩展表 (概念/行业): 先创建 config (含拉取配置), 默认开启定时拉取。
    # 必须在 pull_scheduler.refresh() 之前执行, 否则全新部署时 scheduler 读不到
    # 刚创建的预设, 定时任务不会启动。
    try:
        from app.services.ext_presets import ensure_builtin_presets
        await ensure_builtin_presets(store.data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("内置扩展表初始化失败 (不影响启动): %s", e)

    # 扩展数据定时拉取: 在预设配置就绪后启动, 自动调度 enabled 的预设。
    from app.services.ext_pull import pull_scheduler
    pull_scheduler.start(store.data_dir)
    pull_scheduler.refresh(store.data_dir)
    app.state.pull_scheduler = pull_scheduler

    # 财务数据 (需 Expert 套餐): 仅初始化调度器供 /api/financials/sync/* 手动同步,
    # 不启动自动调度——用户在「财务分析」页点「同步」手动拉取。
    from app.services.financial_sync import financial_scheduler
    financial_scheduler.start(store.data_dir, capset)
    app.state.financial_scheduler = financial_scheduler

    # 策略引擎
    from app.strategy.engine import StrategyEngine
    from app.strategy import config as strategy_config
    from app.strategy.monitor import StrategyMonitorService
    from app.services.screener import ScreenerService

    _screener_svc = ScreenerService(repo)
    _etf_screener_svc = ScreenerService(repo, asset_type="etf")
    strategy_dirs = [
        Path(__file__).resolve().parent / "strategy" / "builtin",
        store.data_dir / "strategies" / "custom",
        store.data_dir / "strategies" / "ai",
        store.data_dir / "strategies" / "composite",
    ]
    strategy_engine = StrategyEngine(
        strategy_dirs=strategy_dirs,
        override_loader=lambda sid: strategy_config.load_override(store.data_dir, sid),
    )
    app.state.strategy_engine = strategy_engine
    logger.info("strategy engine loaded: %d strategies", len(strategy_engine.list_strategies()))

    matrix_prewarm_owner = MatrixCachePrewarmOwner()

    def _schedule_matrix_cache_prewarm() -> None:
        if (
            not settings.backtest_matrix_disk_cache_enabled
            or not settings.backtest_matrix_cache_prewarm
        ):
            return

        def _prewarm() -> None:
            from app.backtest.engine import BacktestEngine
            from app.backtest.matrix import MatrixPrewarmCancelledError
            from app.backtest.strategy import prewarm_matrix_cache
            from app.services.heavy_job_limiter import (
                HeavyJobCancelledError,
                shared_heavy_job_limiter,
            )

            try:
                latest = repo.latest_enriched_date("stock")
                if latest is None:
                    logger.info("matrix cache prewarm skipped: no stock enriched data")
                    return

                with shared_heavy_job_limiter.slot(
                    "normal",
                    cancel_event=matrix_prewarm_owner.cancel_event,
                ):
                    result = prewarm_matrix_cache(
                        BacktestEngine(repo),
                        strategy_engine,
                        asset_type="stock",
                        latest_date=latest,
                        years=settings.backtest_matrix_cache_prewarm_years,
                        cancel_event=matrix_prewarm_owner.cancel_event,
                    )
                logger.info("matrix cache prewarm done: %s", result)
            except (HeavyJobCancelledError, MatrixPrewarmCancelledError):
                logger.info("matrix cache prewarm cancelled")
            except Exception:  # noqa: BLE001
                logger.exception("matrix cache prewarm failed")

        if not matrix_prewarm_owner.schedule(_prewarm):
            logger.info("matrix cache prewarm already running or shutting down, skip")

    repo._on_refresh_done = _schedule_matrix_cache_prewarm  # noqa: SLF001
    if repo.enriched_ready:
        _schedule_matrix_cache_prewarm()

    # 通用监控规则引擎: 启动时 reload 规则到内存态 (修复重启后告警失效)
    from app.strategy.monitor import MonitorRuleEngine
    from app.strategy import monitor_rules as mr_store
    from app.services import preferences
    from app.services.sector_monitor import SectorMonitorService
    monitor_engine = MonitorRuleEngine()
    sector_monitor_service = SectorMonitorService(repo)
    monitor_engine.set_strategy_engine(strategy_engine)
    monitor_engine.set_data_dir(store.data_dir)
    monitor_engine.set_sector_monitor_service(sector_monitor_service)
    # 复用 ScreenerService 的历史窗口加载器 (三级缓存, 启动预计算命中 ~0ms),
    # 让声明 filter_history 的策略 (如反包) 也能在实时监控里跑选股 → 盘中触发通知。
    monitor_engine.set_history_loader(_screener_svc._load_enriched_history)
    # ETF 版历史加载器: asset_type=etf 的 strategy 型规则用 (读 kline_etf_enriched)。
    monitor_engine.set_history_loader_etf(_etf_screener_svc._load_enriched_history)

    # 自动迁移: 把旧 strategy_monitor_ids 同步为 type=strategy 规则 (统一到监控页)
    try:
        if preferences.get_strategy_monitor_enabled():
            ids = preferences.get_strategy_monitor_ids()
            if ids:
                names = {s["id"]: s["name"] for s in strategy_engine.list_strategies()}
                mr_store.migrate_strategy_monitors(store.data_dir, ids, names)
                logger.info("strategy monitor migrated: %d strategies", len(ids))
    except Exception as e:  # noqa: BLE001
        logger.warning("strategy monitor migration failed: %s", e)

    try:
        rules = mr_store.load_all(store.data_dir)
        monitor_engine.set_rules(rules)
        logger.info("monitor engine loaded: %d rules", monitor_engine.rule_count)
    except Exception as e:  # noqa: BLE001
        logger.warning("monitor engine load failed: %s", e)
    app.state.monitor_engine = monitor_engine
    app.state.sector_monitor_service = sector_monitor_service

    # 源码内二次开发启动钩子: 仅暴露稳定只读上下文, 单个扩展失败不影响核心启动。
    extension_registry = app.state.extension_registry
    start_backend_extensions(
        current_extension_context(data_dir=store.data_dir, repository=repo),
        extension_registry,
    )

    try:
        yield
    finally:
        repo._on_refresh_done = None  # noqa: SLF001
        if not matrix_prewarm_owner.shutdown(timeout=5.0):
            logger.warning("matrix cache prewarm did not stop within 5 seconds")
        mmanager = getattr(app.state, "mining_manager", None)
        if mmanager:
            mmanager.shutdown()
        if app.state.scheduler:
            app.state.scheduler.shutdown(wait=False)
        ps = getattr(app.state, "pull_scheduler", None)
        if ps:
            ps.stop()
        fsc = getattr(app.state, "financial_scheduler", None)
        if fsc:
            fsc.stop()
        qs = getattr(app.state, "quote_service", None)
        if qs:
            qs.stop()
        dsvc = getattr(app.state, "depth_service", None)
        if dsvc:
            dsvc.stop_polling()
        wbot = getattr(app.state, "wecom_bot_service", None)
        if wbot:
            wbot.stop()
        mrs = getattr(app.state, "minute_refresh", None)
        if mrs:
            mrs.stop()
        logger.info("shutdown")


@asynccontextmanager
async def lifespan(app: FastAPI):
    mining_process_lock = MiningProcessLock(settings.data_dir)
    mining_process_lock.acquire()
    try:
        async with _application_lifespan(app):
            yield
    finally:
        mining_process_lock.release()


app = FastAPI(
    title="Tick Stock Panel",
    version=__version__,
    description="A 股选股 + 回测面板 — TickFlow 适配",
    lifespan=lifespan,
)

# CORS: 允许局域网访问 (自托管场景, 放开所有来源)
# 注: allow_credentials=True 与 allow_origins=['*'] 不能共存 (浏览器规范),
# 本项目认证走 header (API Key), 不依赖 cookie, 故关闭 credentials 换取通配来源。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================================================================
# 访问认证中间件
# ================================================================
# 拦截所有 /api/ 请求, 三种状态:
#   1. 未设密码 + 本机/内网 → 放行(让本机用户访问面板 + 调 /api/auth/setup 设密码)
#   2. 未设密码 + 公网       → 拒绝(403, 防裸奔也防抢占; 引导本机设密码)
#   3. 已设密码              → 检查 session, 无效则 401(前端跳登录)
# 白名单: /api/auth/* (设密码/登录本身)、/health 等探活。
_AUTH_WHITELIST_PREFIX = ("/api/auth/",)
_AUTH_WHITELIST_EXACT = ("/health", "/api/health", "/openapi.json", "/docs", "/redoc")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # 仅 /api/ 走认证; 静态资源(前端页面/assets)放行, 由前端处理跳转
    if not path.startswith("/api/"):
        return await call_next(request)
    # 白名单放行(设密码/登录/探活本身不拦)
    if path.startswith(_AUTH_WHITELIST_PREFIX) or path in _AUTH_WHITELIST_EXACT:
        return await call_next(request)

    from app.services import auth as auth_service
    # 情况 1+2: 未设密码
    if not auth_service.is_configured():
        # 本机/内网 → 放行(服务器主人可访问, 并去 /login 设密码)
        if auth_api._is_local_network(auth_api._client_ip(request)):
            return await call_next(request)
        # 公网 → 拒绝。不裸奔, 也不给公网设密码的机会(防抢占)
        return JSONResponse(
            status_code=403,
            content={
                "detail": "面板尚未初始化访问密码,请通过 SSH/本机浏览器访问以设置密码",
                "code": "NOT_INITIALIZED",
            },
        )

    # 情况 3: 已设密码, 检查会话
    token = request.cookies.get(auth_api.COOKIE_NAME)
    if token and auth_service.is_valid_session(token):
        return await call_next(request)
    # 未登录: 401(前端跳登录页)
    return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})


# 路由
app.include_router(core_router)
app.include_router(auth_api.router)
app.include_router(kline.router)
app.include_router(watchlist.router)
app.include_router(screener.router)
app.include_router(backtest.router)
app.include_router(factors.router)
app.include_router(mining.router)
app.include_router(intraday.router)
app.include_router(indices.router)
app.include_router(overview.router)
app.include_router(abnormal.router)
app.include_router(regime.router)
app.include_router(analysis.router)
app.include_router(pipeline.router)
app.include_router(data.router)
app.include_router(ext_data.router)
app.include_router(financials.router)
app.include_router(stock_analysis.router)
app.include_router(market_recap.router)
app.include_router(settings_api.router)
app.include_router(strategy.router)
app.include_router(signals.router)
app.include_router(monitor_rules.router)
app.include_router(lots.router)
app.include_router(alerts.router)
app.include_router(rps.router)

# 二次开发路由与小粒度策略在所有核心路由后注册, 禁止覆盖核心路径。
extension_registry, extension_load_errors = configure_backend_extensions(app)
app.state.extension_registry = extension_registry
app.state.extension_load_errors = extension_load_errors


# 能力门控异常 → 403(而非默认 500)
# 业务代码用 capset.require(Cap.X) 断言能力,缺失时抛 CapabilityDenied;
# 若不注册 handler 会冒泡成 500 Internal Server Error,对前端不友好且语义错误。
from fastapi import Request
from fastapi.responses import JSONResponse
from app.tickflow.capabilities import CapabilityDenied


@app.exception_handler(CapabilityDenied)
async def capability_denied_handler(request: Request, exc: CapabilityDenied) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": str(exc), "suggestion": exc.suggestion},
    )

# 生产期静态文件(前端 dist)
_static = Path(settings.static_dir)
if _static.exists():
    if (_static / "assets").exists():
        app.mount("/assets", StaticFiles(directory=_static / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):  # noqa: ARG001
        """所有未匹配路径回退到 index.html — React Router 接管。

        index.html 禁止缓存 (Cache-Control: no-store), 确保浏览器每次拿到
        最新版本引用的 JS/CSS 文件名 (assets 带 hash, 可长缓存)。
        """
        index = _static / "index.html"
        if index.exists():
            return FileResponse(
                index,
                headers={"Cache-Control": "no-store, must-revalidate"},
            )
        return {"error": "frontend not built"}
