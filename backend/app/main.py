"""FastAPI 入口。"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import (
    alerts,
    analysis,
    auth as auth_api,
    backtest,
    data,
    ext_data,
    financials,
    indices,
    intraday,
    invites as invites_api,
    kline,
    market_recap,
    monitor_rules,
    overview,
    pipeline,
    rps,
    screener,
    settings as settings_api,
    signals,
    stock_analysis,
    strategy,
    users as users_api,
    watchlist,
)
from app.api.routes import router as core_router
from app.config import settings
from app.jobs import daily_pipeline
from app.services.quote_service import QuoteService
from app.sycee.router import (
    SyceeStrategyAuthoringGuardMiddleware,
    requires_admin as sycee_requires_admin,
    router as sycee_router,
)
from app.tickflow import client as tf_client
from app.tickflow.capabilities import CapabilityDenied
from app.tickflow.policy import detect_capabilities
from app.tickflow.repository import DataStore, KlineRepository

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "TickFlow Stock Panel v%s starting (mode=%s)",
        __version__, tf_client.current_mode(),
    )

    # 首次启动: 将旧单用户密码迁移为 admin, 或使用 AUTH_PASSWORD 创建管理员。
    admin = None
    try:
        from app.services import users
        admin = users.ensure_bootstrap(settings.data_dir, settings.auth_password)
        if admin:
            copied = users.migrate_legacy_admin_data(settings.data_dir)
            if copied:
                logger.info("legacy admin data migrated: %s", ", ".join(copied))
        users.sync_env_invites(settings.data_dir, settings.invite_codes)
    except Exception as e:  # noqa: BLE001
        logger.warning("account bootstrap failed: %s", e)

    from app.services import invites as invite_service
    invite_store = invite_service.get_store()
    if invite_store.enabled:
        logger.info("private beta invite access enabled (%d slots)", invite_store.capacity)

    # 数据层
    store = DataStore()
    repo = KlineRepository(store)
    app.state.datastore = store
    app.state.repo = repo
    # 在接受回测请求前固定 managed generation，避免首批并发 worker 各自创建版本。
    if settings.backtest_matrix_disk_cache_enabled:
        repo.get_matrix_data_generation("stock")
    # 指标异步预热标志: enriched 缓存在后台线程构建, 完成后置 True
    app.state.indicators_ready = False
    repo._on_warmup_done = lambda: setattr(app.state, "indicators_ready", True)  # noqa: SLF001

    # Polars 缓存预热 — enriched 的重计算 (107万行 compute_indicators) 推后台,
    # instruments/index/ETF 仍同步 (毫秒级)。应用立即 ready, 指标算完后自动替换。
    repo.refresh_cache(background=True)

    # 能力探测
    capset = detect_capabilities()
    app.state.capabilities = capset
    logger.info("ready; %d capabilities active", len(capset.all()))

    # 自定义数据源配置(可选): 失败只记录错误, 不影响 TickFlow 基准路径。
    try:
        from app.data_providers import custom as custom_sources
        custom_sources.load_all()
        logger.info("custom data sources loaded: %d", len(custom_sources.list_sources()))
    except Exception as e:  # noqa: BLE001
        logger.warning("custom data sources init failed: %s", e)

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
    from app.strategy.monitor import StrategyMonitorService
    from app.services.screener import ScreenerService

    _screener_svc = ScreenerService(repo)
    _etf_screener_svc = ScreenerService(repo, asset_type="etf")
    from app.services.user_storage import user_dir
    admin_root = user_dir(store.data_dir, "admin")
    strategy_dirs = [
        Path(__file__).resolve().parent / "strategy" / "builtin",
        admin_root / "strategies" / "custom",
        admin_root / "strategies" / "ai",
    ]
    strategy_engine = StrategyEngine(
        strategy_dirs=strategy_dirs,
    )
    app.state.strategy_engine = strategy_engine
    logger.info("strategy engine loaded: %d strategies", len(strategy_engine.list_strategies()))

    matrix_prewarm_lock = threading.Lock()
    matrix_prewarm_running = False

    def _schedule_matrix_cache_prewarm() -> None:
        nonlocal matrix_prewarm_running
        if (
            not settings.backtest_matrix_disk_cache_enabled
            or not settings.backtest_matrix_cache_prewarm
        ):
            return
        with matrix_prewarm_lock:
            if matrix_prewarm_running:
                logger.info("matrix cache prewarm already in progress, skip")
                return
            matrix_prewarm_running = True

        def _prewarm() -> None:
            nonlocal matrix_prewarm_running
            try:
                latest = repo.latest_enriched_date("stock")
                if latest is None:
                    logger.info("matrix cache prewarm skipped: no stock enriched data")
                    return
                from app.backtest.engine import BacktestEngine
                from app.backtest.strategy import prewarm_matrix_cache

                result = prewarm_matrix_cache(
                    BacktestEngine(repo),
                    strategy_engine,
                    asset_type="stock",
                    latest_date=latest,
                    years=settings.backtest_matrix_cache_prewarm_years,
                )
                logger.info("matrix cache prewarm done: %s", result)
            except Exception:  # noqa: BLE001
                logger.exception("matrix cache prewarm failed")
            finally:
                with matrix_prewarm_lock:
                    matrix_prewarm_running = False

        threading.Thread(
            target=_prewarm,
            name="matrix-cache-prewarm",
            daemon=True,
        ).start()

    repo._on_refresh_done = _schedule_matrix_cache_prewarm  # noqa: SLF001
    if repo.enriched_ready:
        _schedule_matrix_cache_prewarm()

    # 通用监控规则引擎: 启动时 reload 规则到内存态 (修复重启后告警失效)
    from app.strategy.monitor import MonitorRuleEngine
    from app.strategy import monitor_rules as mr_store
    from app.services import preferences
    monitor_engine = MonitorRuleEngine()
    monitor_engine.set_strategy_engine(strategy_engine)
    monitor_engine.set_data_dir(store.data_dir)
    # 复用 ScreenerService 的历史窗口加载器 (三级缓存, 启动预计算命中 ~0ms),
    # 让声明 filter_history 的策略 (如反包) 也能在实时监控里跑选股 → 盘中触发通知。
    monitor_engine.set_history_loader(_screener_svc._load_enriched_history)
    # ETF 版历史加载器: asset_type=etf 的 strategy 型规则用 (读 kline_etf_enriched)。
    monitor_engine.set_history_loader_etf(_etf_screener_svc._load_enriched_history)

    from app.services import user_context, users
    from app.services.user_runtime import UserRuntime, UserRuntimeRegistry

    def _load_monitor_rules(identity, engine, strategies) -> None:
        with user_context.as_user(identity):
            try:
                if preferences.get_strategy_monitor_enabled():
                    ids = preferences.get_strategy_monitor_ids()
                    if ids:
                        names = {s["id"]: s["name"] for s in strategies.list_strategies()}
                        mr_store.migrate_strategy_monitors(store.data_dir, ids, names)
            except Exception as exc:  # noqa: BLE001
                logger.warning("strategy monitor migration failed for %s: %s", identity.username, exc)
            engine.set_rules(mr_store.load_all(store.data_dir))

    admin = admin or users.get_user(store.data_dir, "admin")
    if admin:
        _load_monitor_rules(admin, monitor_engine, strategy_engine)
    app.state.monitor_engine = monitor_engine

    def _build_user_runtime(identity):
        root = user_dir(store.data_dir, identity.id)
        per_user_strategy_engine = StrategyEngine(strategy_dirs=[
            Path(__file__).resolve().parent / "strategy" / "builtin",
            root / "strategies" / "custom",
            root / "strategies" / "ai",
        ])
        per_user_monitor_engine = MonitorRuleEngine()
        per_user_monitor_engine.set_strategy_engine(per_user_strategy_engine)
        per_user_monitor_engine.set_data_dir(store.data_dir)
        per_user_monitor_engine.set_history_loader(_screener_svc._load_enriched_history)
        per_user_monitor_engine.set_history_loader_etf(_etf_screener_svc._load_enriched_history)
        _load_monitor_rules(identity, per_user_monitor_engine, per_user_strategy_engine)
        return UserRuntime(identity, per_user_strategy_engine, per_user_monitor_engine)

    runtime_registry = UserRuntimeRegistry(_build_user_runtime)
    if admin:
        runtime_registry.register(UserRuntime(admin, strategy_engine, monitor_engine))
    for row in users.list_users(store.data_dir):
        identity = users.get_user(store.data_dir, row["id"])
        if identity and (not admin or identity.id != admin.id):
            runtime_registry.get(identity)
    app.state.user_runtime_registry = runtime_registry

    yield

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
    logger.info("shutdown")


app = FastAPI(
    title="TickFlow Stock Panel",
    version=__version__,
    description="A 股选股 + 回测面板 — TickFlow 适配",
    lifespan=lifespan,
)

app.add_middleware(SyceeStrategyAuthoringGuardMiddleware)

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
# 白名单: 账户入口、明确的公开分享和 /health 等探活。
_AUTH_WHITELIST_PREFIX = (
    "/api/auth/",
    "/api/invite/",
    "/api/public/sycee/research/",
)
_AUTH_WHITELIST_EXACT = ("/health", "/api/health", "/openapi.json", "/docs", "/redoc")
_INVITE_PUBLIC_PREFIX = ("/api/invite/", "/assets/")
_INVITE_PUBLIC_EXACT = ("/invite", "/favicon.svg", "/health", "/api/health")

_ADMIN_PREFIXES = (
    "/api/admin/",
    "/api/settings/data-sources",
    "/api/settings/plugins/",
    "/api/settings/tickflow-key",
    "/api/settings/switch_endpoint",
    "/api/settings/test_endpoint",
)
_ADMIN_MUTATION_PREFIXES = (
    "/api/pipeline",
    "/api/ext-data",
    "/api/indices/sync",
    "/api/financials/sync",
    "/api/data/clear",
    "/api/data/refresh-cache",
    "/api/settings/ai",
    "/api/settings/preferences/data-providers",
    "/api/settings/preferences/minute-sync",
    "/api/settings/preferences/realtime-quotes",
    "/api/settings/preferences/realtime-quote-scope",
    "/api/settings/preferences/quote-interval",
    "/api/settings/preferences/pipeline-",
    "/api/settings/preferences/instruments-schedule",
    "/api/settings/preferences/enriched-batch-size",
    "/api/settings/preferences/index-daily-batch-size",
    "/api/settings/preferences/limit-ladder-monitor",
    "/api/settings/preferences/depth-",
    "/api/settings/preferences/review-schedule",
)


def _admin_required(path: str, method: str) -> bool:
    if sycee_requires_admin(path, method):
        return True
    if path.startswith(_ADMIN_PREFIXES):
        return True
    return method not in {"GET", "HEAD", "OPTIONS"} and path.startswith(_ADMIN_MUTATION_PREFIXES)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS":
        return await call_next(request)
    # 仅 /api/ 走认证; 静态资源(前端页面/assets)放行, 由前端处理跳转。
    if not path.startswith("/api/"):
        return await call_next(request)
    # 账户、邀请码注册和探活本身不拦。
    if path.startswith(_AUTH_WHITELIST_PREFIX) or path in _AUTH_WHITELIST_EXACT:
        return await call_next(request)

    from app.services import user_context, users
    user = users.user_for_session(settings.data_dir, request.cookies.get(auth_api.COOKIE_NAME))
    if user is None:
        if users.has_available_invites(settings.data_dir):
            return JSONResponse(
                status_code=403,
                content={"detail": "请先完成邀请码注册", "code": "INVITE_REQUIRED"},
            )
        if not users.is_configured(settings.data_dir):
            if auth_api._is_local_network(auth_api._client_ip(request)):
                return await call_next(request)
            return JSONResponse(
                status_code=403,
                content={"detail": "面板尚未初始化管理员账户,请通过本机或内网访问", "code": "NOT_INITIALIZED"},
            )
        return JSONResponse(
            status_code=401,
            content={"detail": "未登录或会话已过期,请重新登录", "code": "AUTH_REQUIRED"},
        )

    token = user_context.set_current_user(user)
    request.state.user = user
    try:
        if _admin_required(path, request.method) and not user.is_admin:
            return JSONResponse(
                status_code=403,
                content={"detail": "仅管理员可执行此操作", "code": "ADMIN_REQUIRED"},
            )
        return await call_next(request)
    finally:
        user_context.reset(token)


# 路由
app.include_router(core_router)
app.include_router(invites_api.router)
app.include_router(auth_api.router)
app.include_router(users_api.router)
app.include_router(kline.router)
app.include_router(watchlist.router)
app.include_router(screener.router)
app.include_router(backtest.router)
app.include_router(intraday.router)
app.include_router(indices.router)
app.include_router(overview.router)
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
app.include_router(alerts.router)
app.include_router(rps.router)
app.include_router(sycee_router)


# 能力门控异常 → 403(而非默认 500)
# 业务代码用 capset.require(Cap.X) 断言能力,缺失时抛 CapabilityDenied;
# 若不注册 handler 会冒泡成 500 Internal Server Error,对前端不友好且语义错误。
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
