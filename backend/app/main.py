"""FastAPI 入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import agent, analysis, auth as auth_api, backtest, backtest_parameter_grid, cross_section, data, documents, ext_data, financials, indices, intraday, kline, market_recap, monitor_rules, alerts, overview, patterns, pipeline, regime, research, review, rps, screener, settings as settings_api, signal_scorecard, signals, stock_analysis, strategy, strategy_profile, trade_journal, trading, trading_plans, trading_review, watchlist
from app.api.routes import router as core_router
from app.config import settings
from app.log_redaction import install_secret_redaction_filter
from app.data_providers.capability_gate import detect_capabilities
from app.jobs import daily_pipeline
from app.data_providers.registry import close_all_providers
from app.services.data_mode import current_data_mode
from app.services.quote_service import QuoteService
from app.services.screener import close_screener_sql_connection
from app.storage.repository import DataStore, KlineRepository

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
install_secret_redaction_filter()
logger = logging.getLogger(__name__)


def _run_shutdown_step(name: str, callback) -> None:
    try:
        callback()
    except Exception:  # noqa: BLE001
        logger.exception("shutdown step failed: %s", name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "TickFlow Stock Panel v%s starting (mode=%s)",
        __version__, current_data_mode(),
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

    # Polars 缓存预热
    repo.refresh_cache()

    # 能力探测
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
        daily_pipeline.start_local_enriched_bootstrap(repo, capset)
    except Exception as e:  # noqa: BLE001
        logger.warning("scheduler not started: %s", e)
        app.state.scheduler = None

    # depth sealed: 启动补跑(当天文件不存在) + 盘中轮询(有能力时)
    try:
        depth_service.boot_check()
        depth_service.start_polling()
    except Exception as e:  # noqa: BLE001
        logger.warning("depth_service init failed: %s", e)

    # 扩展数据定时拉取
    from app.services.ext_pull import pull_scheduler
    pull_scheduler.start(store.data_dir)
    pull_scheduler.refresh(store.data_dir)
    app.state.pull_scheduler = pull_scheduler

    # 内置扩展表 (概念/行业): 只创建 config (含拉取配置), 不自动拉数据
    # 数据获取由用户在概念/行业页点「获取数据」手动触发 (POST /api/ext-data/presets/{id}/fetch)
    try:
        from app.services.ext_presets import ensure_builtin_presets
        await ensure_builtin_presets(store.data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("内置扩展表初始化失败 (不影响启动): %s", e)

    # 财务数据 (需 FINANCIAL capability): 仅初始化调度器供 /api/financials/sync/* 手动同步,
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
    strategy_dirs = [
        Path(__file__).resolve().parent / "strategy" / "builtin",
        store.data_dir / "strategies" / "custom",
        store.data_dir / "strategies" / "ai",
        store.data_dir / "strategies" / "composite",
    ]
    strategy_engine = StrategyEngine(
        enriched_loader=_screener_svc._load_enriched_for_date,
        enriched_history_loader=_screener_svc._load_enriched_history,
        strategy_dirs=strategy_dirs,
        override_loader=lambda strategy_id: strategy_config.load_override(
            store.data_dir, strategy_id
        ),
    )
    app.state.strategy_engine = strategy_engine
    logger.info("strategy engine loaded: %d strategies", len(strategy_engine.list_strategies()))

    try:
        from app.services.scheduled_research import ScheduledResearchStore, register_jobs
        register_jobs(app.state.scheduler, ScheduledResearchStore(store.data_dir), app.state)
    except Exception as e:  # noqa: BLE001
        logger.warning("scheduled research registration failed: %s", e)

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

    # 自动迁移: 把旧 strategy_monitor_ids 同步为 type=strategy 规则 (统一到监控页)
    try:
        if preferences.get_strategy_monitor_enabled():
            ids = preferences.get_strategy_monitor_ids()
            if ids:
                names = {s.id: s.name for s in strategy_engine.list_strategies()}
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

    try:
        yield
    finally:
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler:
            _run_shutdown_step("scheduler", lambda: scheduler.shutdown(wait=False))
        ps = getattr(app.state, "pull_scheduler", None)
        if ps:
            _run_shutdown_step("pull scheduler", ps.stop)
        fsc = getattr(app.state, "financial_scheduler", None)
        if fsc:
            _run_shutdown_step("financial scheduler", fsc.stop)
        quote_service = getattr(app.state, "quote_service", None)
        if quote_service:
            _run_shutdown_step("quote service", quote_service.stop)
        depth = getattr(app.state, "depth_service", None)
        if depth:
            _run_shutdown_step("depth service", depth.stop_polling)
        _run_shutdown_step("data providers", close_all_providers)
        _run_shutdown_step("screener SQL connection", close_screener_sql_connection)
        _run_shutdown_step("data store", store.close)
        logger.info("shutdown")


app = FastAPI(
    title="TickFlow Stock Panel",
    version=__version__,
    description="A 股选股 + 回测面板",
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
app.include_router(backtest_parameter_grid.router)
app.include_router(intraday.router)
app.include_router(indices.router)
app.include_router(overview.router)
app.include_router(analysis.router)
app.include_router(agent.router)
app.include_router(pipeline.router)
app.include_router(research.router)
app.include_router(cross_section.router)
app.include_router(data.router)
app.include_router(documents.router)
app.include_router(ext_data.router)
app.include_router(financials.router)
app.include_router(stock_analysis.router)
app.include_router(market_recap.router)
app.include_router(review.router)
app.include_router(settings_api.router)
app.include_router(strategy.router)
app.include_router(regime.router)
app.include_router(signals.router)
app.include_router(signal_scorecard.router)
app.include_router(monitor_rules.router)
app.include_router(alerts.router)
app.include_router(rps.router)
app.include_router(patterns.router)
app.include_router(trade_journal.router)
app.include_router(trading.router)
app.include_router(trading_plans.router)
app.include_router(trading_review.router)
app.include_router(strategy_profile.router)


# 能力门控异常 → 403(而非默认 500)
# 业务代码用 capset.require(Cap.X) 断言能力,缺失时抛 CapabilityDenied;
# 若不注册 handler 会冒泡成 500 Internal Server Error,对前端不友好且语义错误。
from fastapi import Request
from fastapi.responses import JSONResponse
from app.capabilities import CapabilityDenied
from app.data_providers.fquant.catalog_resolver import CatalogError, StaleCatalogError
from app.errors import AppError, app_error_handler

# 统一失败语义 (data_incomplete/stale_input/kernel_not_ready 等) → 422 + {code, detail}
app.add_exception_handler(AppError, app_error_handler)


@app.exception_handler(CapabilityDenied)
async def capability_denied_handler(request: Request, exc: CapabilityDenied) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": str(exc), "suggestion": exc.suggestion},
    )


# DuckDB 目录异常 → 503(而非默认 500,也绝不是"空数据")
# engine 每天发布数据快照后会重新发布路由目录;在两者之间、以及目录发布失败时,
# 目录仍钉着旧 generation,读取会 fail closed 抛 StaleCatalogError。这是暂时性的、
# 可恢复的状态,所以是 503 + Retry-After,不是 500。
# 关键是它必须可见:若吞成空结果,前端看到的空图和"这天休市"无法区分。
@app.exception_handler(CatalogError)
async def catalog_error_handler(request: Request, exc: CatalogError) -> JSONResponse:
    stale = isinstance(exc, StaleCatalogError)
    return JSONResponse(
        status_code=503,
        content={
            "detail": str(exc),
            "code": "catalog_stale" if stale else "catalog_unavailable",
            "suggestion": (
                "数据快照目录尚未刷新到最新 generation(engine 的 publish.catalog 可能还没跑完或失败了)。"
                "稍后重试;若持续出现,检查 engine 调度的 publish.catalog 节点。"
                if stale
                else "数据快照目录不可读或已损坏,请检查 engine 发布的 catalog 快照。"
            ),
        },
        headers={"Retry-After": "60"} if stale else None,
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
