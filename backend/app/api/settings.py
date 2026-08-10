"""设置 API — Key 配置 / 模式切换。

提供面向非开发者的 UI 配置入口,避免逼用户改 .env。
"""
from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import secrets_store
from app.data_providers.capability_gate import (
    detect_capabilities,
    tier_label,
)
from app.services.data_mode import current_data_mode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

def _sync_financial_scheduler_caps(app_state, capset) -> None:
    """把重新探测出的能力同步给财务调度器。

    app.state.capabilities 在此已更新, 但 FinancialScheduler 在启动时捕获的是旧引用,
    需显式刷新, 否则用户升级到 Expert 后点「全部同步」仍会因调度器读旧 capset 而被拒。
    """
    fs = getattr(app_state, "financial_scheduler", None)
    if fs is None:
        return
    try:
        fs.update_capabilities(capset)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("update financial_scheduler capabilities failed: %s", e)


@router.get("")
def get_settings() -> dict:
    """返回当前配置概况(Key 脱敏)。"""
    from app.config import settings
    from app.data_providers.registry import get_active_provider_name
    from app.services import preferences
    from app.services.ai_provider import ai_configured, current_ai_model, current_codex_command

    ai_provider = secrets_store.get_ai_config("ai_provider", settings.ai_provider)
    return {
        "mode": current_data_mode(),
        "data_provider": get_active_provider_name(),
        # 首次使用引导
        "onboarding_completed": preferences.get_onboarding_completed(),
        # AI 配置
        "ai_provider": ai_provider,
        "ai_base_url": secrets_store.get_ai_config("ai_base_url", settings.ai_base_url),
        "ai_api_key_masked": secrets_store.mask(secrets_store.get_ai_key()),
        "has_ai_key": bool(secrets_store.get_ai_key()),
        "ai_configured": ai_configured(ai_provider),
        "ai_model": current_ai_model(),
        "ai_codex_command": current_codex_command(),
        "ai_user_agent": secrets_store.get_ai_config("ai_user_agent", settings.ai_user_agent),
    }


@router.post("/onboarding/complete")
def complete_onboarding() -> dict:
    """标记首次使用向导完成。

    写入 preferences.json,前端守卫据此判断是否需要再次展示向导。
    跨设备/清缓存安全 —— 状态落在后端文件,不依赖浏览器本地存储。
    """
    from app.services import preferences
    done = preferences.set_onboarding_completed(True)
    return {"ok": True, "onboarding_completed": done}


class AiSettingsIn(BaseModel):
    provider: str = "openai_compat"
    base_url: str = ""
    api_key: str | None = None
    model: str = ""
    codex_command: str = ""
    user_agent: str = ""


class AiProfileIn(BaseModel):
    name: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    codex_command: str | None = None
    launch_command: str | None = None
    user_agent: str | None = None


@router.post("/ai")
def save_ai_settings(req: AiSettingsIn) -> dict:
    """保存 AI 配置（全部持久化到 secrets.json）"""
    from app.config import settings
    from app.services.ai_provider import ai_configured, current_ai_model, current_ai_provider, current_codex_command, normalize_codex_command

    updates: dict = {}
    if req.provider:
        updates["ai_provider"] = req.provider
        settings.ai_provider = req.provider
    if req.base_url:
        updates["ai_base_url"] = req.base_url
        settings.ai_base_url = req.base_url
    if req.api_key is not None:
        if req.api_key:
            updates["ai_api_key"] = req.api_key
            settings.ai_api_key = req.api_key
        else:
            secrets_store.clear("ai_api_key")
            settings.ai_api_key = ""
    if req.provider == "codex_cli" and not req.model:
        secrets_store.clear("ai_model")
        settings.ai_model = ""
    elif req.model:
        updates["ai_model"] = req.model
        settings.ai_model = req.model
    if req.provider == "codex_cli":
        try:
            codex_command = normalize_codex_command(req.codex_command)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        updates["ai_codex_command"] = codex_command
        settings.ai_codex_command = codex_command
    # user_agent 允许清空(回到默认浏览器 UA),故无条件持久化
    updates["ai_user_agent"] = req.user_agent
    settings.ai_user_agent = req.user_agent

    if updates:
        secrets_store.save(updates)

    provider = current_ai_provider()
    return {
        "ok": True,
        "ai_provider": provider,
        "ai_model": current_ai_model(),
        "ai_codex_command": current_codex_command(),
        "ai_configured": ai_configured(provider),
    }


@router.delete("/ai")
def clear_ai_settings() -> dict:
    """一键清空 AI 配置(provider / base_url / api_key / model)。

    保留 ai_user_agent —— 自定义请求头与凭证解耦,清空凭证不影响绕过 CDN 拦截的设置。
    """
    from app.config import settings

    secrets_store.clear("ai_provider", "ai_base_url", "ai_api_key", "ai_model", "ai_codex_command")
    # 同步重置运行时内存(provider 回默认值,其余置空)
    settings.ai_provider = "openai_compat"
    settings.ai_base_url = ""
    settings.ai_api_key = ""
    settings.ai_model = ""
    settings.ai_codex_command = "codex"

    return {"ok": True}


@router.get("/ai/profiles")
def list_ai_profiles() -> dict:
    from app.services import ai_profiles, ai_routing
    from app.services.ai_usage_snapshot import usage_snapshot

    registry = ai_routing.get_health_registry()
    profiles = ai_profiles.list_profiles_masked()
    # M9: 只读 health snapshot, 无凭据/无 prompt; 每个 profile 附加 in-memory 健康态。
    for p in profiles:
        p["health"] = registry.get_health(p["id"])
    return {
        "profiles": profiles,
        "default_id": ai_profiles.get_default_profile_id(),
        "route_policy": ai_routing.load_route_policy().__dict__,
        "usage_snapshot": usage_snapshot(),
    }


class AiRoutePolicyIn(BaseModel):
    allow_profile_fallback: bool = False
    fallback_profile_ids: list[str] = []


@router.put("/ai/route-policy")
def update_ai_route_policy(req: AiRoutePolicyIn) -> dict:
    """保存 AI profile 受控 fallback 策略。

    默认关闭；仅用户显式开启后才按 allowlist 顺序切换。
    不存在/非法 profile id 直接 400，不做静默过滤。
    """
    from app.services import ai_profiles, ai_routing

    available = set(ai_profiles.list_profile_ids())
    try:
        policy = ai_routing.validate_route_policy(
            req.allow_profile_fallback, req.fallback_profile_ids, available
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = ai_routing.save_route_policy(policy)
    return {"route_policy": saved.__dict__}


@router.post("/ai/profiles")
def create_ai_profile(req: AiProfileIn) -> dict:
    from app.services import ai_profiles
    data = req.model_dump(exclude_unset=True)
    if not (data.get("name") or "").strip():
        raise HTTPException(status_code=400, detail="AI 配置名称不能为空")
    profile = ai_profiles.create_profile(**data)
    return {"id": profile["id"]}


@router.put("/ai/profiles/{profile_id}")
def update_ai_profile(profile_id: str, req: AiProfileIn) -> dict:
    from app.services import ai_profiles
    data = req.model_dump(exclude_unset=True)
    if not data.get("api_key"):
        data.pop("api_key", None)
    try:
        ai_profiles.update_profile(profile_id, **data)
    except KeyError:
        raise HTTPException(status_code=404, detail="AI 配置不存在")
    return {"ok": True}


@router.delete("/ai/profiles/{profile_id}")
def delete_ai_profile(profile_id: str) -> dict:
    from app.services import ai_profiles
    ai_profiles.delete_profile(profile_id)
    return {"ok": True}


@router.post("/ai/profiles/{profile_id}/default")
def set_default_ai_profile(profile_id: str) -> dict:
    from app.services import ai_profiles
    try:
        ai_profiles.set_default(profile_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="AI 配置不存在")
    return {"ok": True}


@router.post("/ai/profiles/{profile_id}/test")
async def test_ai_profile(profile_id: str) -> dict:
    from app.services.ai_provider import generate_ai_text
    try:
        text = await generate_ai_text(
            [{"role": "user", "content": "Reply exactly: OK"}],
            profile_id=profile_id,
            temperature=0,
            max_tokens=8,
            timeout=15,
        )
        return {"ok": True, "response": text[:80]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ===== 偏好设置 =====

def _realtime_allowed() -> bool:
    """当前数据源/档位是否允许实时行情。"""
    from app.services.quote_service import QuoteService
    return QuoteService.is_realtime_allowed()


def _reset_data_provider_singletons() -> None:
    """Clear service-level provider singletons after a runtime provider switch."""
    from app.services import financial_sync, instrument_sync, kline_sync, quote_service
    financial_sync._provider_instance = None
    instrument_sync._provider_instance = None
    kline_sync._provider_instance = None
    quote_service._provider_instance = None


class MinuteSyncPrefs(BaseModel):
    minute_sync_enabled: bool
    minute_sync_days: int = 5


class DataProviderPrefs(BaseModel):
    data_provider: str


@router.get("/preferences")
def get_preferences() -> dict:
    """返回用户偏好设置。"""
    from app.data_providers.registry import get_active_provider_name
    from app.services import preferences
    env_provider = os.environ.get("DATA_PROVIDER")
    return {
        "data_provider": preferences.get_data_provider(),
        "effective_data_provider": get_active_provider_name(),
        "data_provider_env_override": bool(env_provider),
        "realtime_quotes_enabled": preferences.get_realtime_quotes_enabled(),
        "realtime_allowed": _realtime_allowed(),
        "indices_nav_pinned": preferences.get_indices_nav_pinned(),
        "minute_sync_enabled": preferences.get_minute_sync_enabled(),
        "minute_sync_days": preferences.get_minute_sync_days(),
        "daily_data_provider": preferences.get_daily_data_provider(),
        "adj_factor_provider": preferences.get_adj_factor_provider(),
        "minute_data_provider": preferences.get_minute_data_provider(),
        "realtime_data_provider": preferences.get_realtime_data_provider(),
        "financial_data_provider": preferences.get_financial_data_provider(),
        "depth_data_provider": preferences.get_depth_data_provider(),
        "realtime_watchlist_symbols": preferences.get_realtime_watchlist_symbols(),
        **preferences.get_realtime_quote_scope(),
        "pipeline_pull_a_share": preferences.get_pipeline_pull_a_share(),
        "pipeline_pull_etf": preferences.get_pipeline_pull_etf(),
        "pipeline_pull_index": preferences.get_pipeline_pull_index(),
        "pipeline_pull_hk": preferences.get_pipeline_pull_hk(),
        "pipeline_index_symbols": preferences.get_pipeline_index_symbols(),
        "pipeline_schedule": preferences.get_pipeline_schedule(),
        "instruments_schedule": preferences.get_instruments_schedule(),
        "enriched_batch_size": preferences.get_enriched_batch_size(),
        "index_daily_batch_size": preferences.get_index_daily_batch_size(),
        "watchlist_columns": preferences.get_watchlist_columns(),
        "screener_result_columns": preferences.get_screener_result_columns(),
        "sse_refresh_pages": preferences.get_sse_refresh_pages(),
        "strategy_monitor_enabled": preferences.get_strategy_monitor_enabled(),
        "strategy_monitor_ids": preferences.get_strategy_monitor_ids(),
        "system_notify_enabled": preferences.get_system_notify_enabled(),
        "feishu_webhook_url": preferences.get_feishu_webhook_url(),
        "feishu_webhook_secret": preferences.get_feishu_webhook_secret(),
        "webhook_channels": preferences.get_webhook_channels(),
        "webhook_enabled_default": preferences.get_webhook_enabled_default(),
        "sidebar_index_symbols": preferences.get_sidebar_index_symbols(),
        "nav_order": preferences.get_nav_order(),
        "nav_hidden": preferences.get_nav_hidden(),
        "screener_auto_run": preferences.get_screener_auto_run(),
        "limit_ladder_monitor_enabled": preferences.get_limit_ladder_monitor_enabled(),
        "depth_polling_interval": preferences.get_depth_polling_interval(),
        "depth_finalize_time": preferences.get_depth_finalize_time(),
        "review_schedule": preferences.get_review_schedule(),
        "review_push_channels": preferences.get_review_push_channels(),
        "tradingAutoReview": preferences.get_trading_auto_review(),
        "structured_plan_check_enabled": preferences.get_structured_plan_check_enabled(),
        "external_fallback_enabled": preferences.get_external_fallback_enabled(),
        "external_fallback_scopes": preferences.get_external_fallback_scopes(),
    }


@router.put("/preferences/data-provider")
def update_data_provider(req: DataProviderPrefs, request: Request) -> dict:
    """保存全局数据源偏好。

    DATA_PROVIDER 环境变量优先级最高；存在时本接口仍保存偏好,但不会改变当前进程的有效 provider。
    """
    from app.data_providers.registry import get_active_provider_name
    from app.services import preferences

    try:
        saved = preferences.set_data_provider(req.data_provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    env_provider = os.environ.get("DATA_PROVIDER")
    if not env_provider:
        _reset_data_provider_singletons()
        capset = detect_capabilities(force=True)
        request.app.state.capabilities = capset
        _sync_financial_scheduler_caps(request.app.state, capset)

        qs = getattr(request.app.state, "quote_service", None)
        if qs and not qs.is_realtime_allowed():
            qs.disable()

    return {
        "data_provider": saved,
        "effective_data_provider": get_active_provider_name(),
        "data_provider_env_override": bool(env_provider),
        "mode": current_data_mode(),
        "tier_label": tier_label(),
        "realtime_allowed": _realtime_allowed(),
    }


@router.get("/preferences/watchlist-columns")
def get_watchlist_columns() -> dict:
    """返回自选列表列配置。"""
    from app.services import preferences
    cols = preferences.get_watchlist_columns()
    return {"columns": cols}


class NavOrderIn(BaseModel):
    nav_order: list[str]


class NavHiddenIn(BaseModel):
    nav_hidden: list[str]


@router.put("/preferences/nav-order")
def update_nav_order(req: NavOrderIn) -> dict:
    """保存左侧菜单排序（内置页面 path + 扩展分析菜单 id 的有序列表）。"""
    from app.services import preferences
    saved = preferences.set_nav_order(req.nav_order)
    return {"nav_order": saved}


@router.put("/preferences/nav-hidden")
def update_nav_hidden(req: NavHiddenIn) -> dict:
    """保存左侧菜单隐藏项。"""
    from app.services import preferences
    saved = preferences.set_nav_hidden(req.nav_hidden)
    return {"nav_hidden": saved}


@router.put("/preferences/watchlist-columns")
def update_watchlist_columns(req: dict) -> dict:
    """保存自选列表列配置。"""
    from app.services import preferences
    columns = req.get("columns", [])
    saved = preferences.set_watchlist_columns(columns)
    return {"columns": saved}


@router.get("/preferences/screener-result-columns")
def get_screener_result_columns() -> dict:
    """返回策略结果列表列配置。"""
    from app.services import preferences
    cols = preferences.get_screener_result_columns()
    return {"columns": cols}


@router.put("/preferences/screener-result-columns")
def update_screener_result_columns(req: dict) -> dict:
    """保存策略结果列表列配置。"""
    from app.services import preferences
    columns = req.get("columns", [])
    saved = preferences.set_screener_result_columns(columns)
    return {"columns": saved}


@router.put("/preferences/minute-sync")
def update_minute_sync(req: MinuteSyncPrefs) -> dict:
    """保存分钟 K 同步偏好。"""
    from app.services import preferences
    days = max(1, min(30, req.minute_sync_days))
    preferences.save({
        "minute_sync_enabled": req.minute_sync_enabled,
        "minute_sync_days": days,
    })
    return {
        "minute_sync_enabled": req.minute_sync_enabled,
        "minute_sync_days": days,
    }


class RealtimeQuotesPrefs(BaseModel):
    realtime_quotes_enabled: bool


class RealtimeQuoteScopePrefs(BaseModel):
    realtime_pull_stock: bool | None = None
    realtime_pull_etf: bool | None = None
    realtime_pull_index: bool | None = None
    realtime_index_mode: str | None = None
    realtime_index_symbols: list[str] | None = None


@router.put("/preferences/realtime-quotes")
def update_realtime_quotes(req: RealtimeQuotesPrefs, request: Request) -> dict:
    """保存全局实时行情开关。

    provider 无 realtime 能力时不可开启；支持 realtime 的 provider 开启全市场实时。
    前端据此把开关置灰 / 回弹。
    """
    from app.services import preferences
    qs = getattr(request.app.state, "quote_service", None)

    allowed = qs.is_realtime_allowed() if qs else True
    if req.realtime_quotes_enabled and not allowed:
        # 当前数据源/档位不允许开启实时行情 — 强制关闭
        preferences.save({"realtime_quotes_enabled": False})
        if qs:
            qs.disable()
        return {"realtime_quotes_enabled": False, "realtime_allowed": False}
    if req.realtime_quotes_enabled and qs and qs.realtime_mode() == "watchlist" and not preferences.get_realtime_watchlist_symbols():
        preferences.save({"realtime_quotes_enabled": False})
        return {"realtime_quotes_enabled": False, "realtime_allowed": True, "mode": "watchlist", "error": "watchlist_empty"}

    preferences.save({"realtime_quotes_enabled": req.realtime_quotes_enabled})
    if qs:
        if req.realtime_quotes_enabled:
            qs.enable()
        else:
            qs.disable()

    return {"realtime_quotes_enabled": req.realtime_quotes_enabled, "realtime_allowed": allowed}


@router.put("/preferences/realtime-quote-scope")
def update_realtime_quote_scope(req: RealtimeQuoteScopePrefs) -> dict:
    """保存盘中实时行情范围；独立于盘后管道范围。"""
    from app.services import preferences
    cfg = req.model_dump(exclude_none=True)
    return preferences.set_realtime_quote_scope(cfg)


class RealtimeWatchlistPrefs(BaseModel):
    symbols: list[str] = []


@router.put("/preferences/realtime-watchlist")
def update_realtime_watchlist(req: RealtimeWatchlistPrefs) -> dict:
    """兼容旧入口；Free 实时标的由自选页前 5 个决定。"""
    from app.services import preferences
    symbols = preferences.set_realtime_watchlist_symbols(req.symbols)
    return {"realtime_watchlist_symbols": symbols}


class IndicesNavPinnedPrefs(BaseModel):
    indices_nav_pinned: bool


@router.put("/preferences/indices-nav-pinned")
def update_indices_nav_pinned(req: IndicesNavPinnedPrefs) -> dict:
    """保存侧栏指数报价卡片固定显示开关。
    ON=常驻显示；OFF=跟随实时行情开关（仅实时开时显示）。"""
    from app.services import preferences
    preferences.save({"indices_nav_pinned": req.indices_nav_pinned})
    return {"indices_nav_pinned": req.indices_nav_pinned}


class RealtimeMonitorConfigIn(BaseModel):
    sse_refresh_pages: dict[str, bool] | None = None
    strategy_monitor_enabled: bool | None = None
    strategy_monitor_ids: list[str] | None = None
    sidebar_index_symbols: list[str] | None = None
    screener_auto_run: bool | None = None


@router.put("/preferences/realtime-monitor")
def update_realtime_monitor_config(req: RealtimeMonitorConfigIn, request: Request) -> dict:
    """更新实时监控配置。策略监控统一迁移为 MonitorRule,由监控引擎评估。"""
    from app.services import preferences

    cfg = req.model_dump(exclude_none=True)
    result = preferences.set_realtime_monitor_config(cfg)

    # 策略监控开关/池变化 → 同步迁移为 type=strategy 规则 + reload 引擎
    if req.strategy_monitor_ids is not None or req.strategy_monitor_enabled is not None:
        monitor_engine = getattr(request.app.state, "monitor_engine", None)
        strategy_engine = getattr(request.app.state, "strategy_engine", None)
        data_dir = request.app.state.repo.store.data_dir
        if monitor_engine is not None and strategy_engine is not None:
            from app.strategy import monitor_rules as mr_store
            try:
                if preferences.get_strategy_monitor_enabled():
                    ids = preferences.get_strategy_monitor_ids()
                    names = {s.id: s.name for s in strategy_engine.list_strategies()}
                    mr_store.migrate_strategy_monitors(data_dir, ids, names)
                else:
                    # 关闭策略监控: 停用所有策略规则
                    mr_store.migrate_strategy_monitors(data_dir, [], {})
                # reload 规则到引擎
                monitor_engine.set_rules(mr_store.load_all(data_dir))
            except Exception:
                pass

    return result


class PipelinePullTypesIn(BaseModel):
    """盘后管道拉取内容开关(A股 / ETF / 指数 / 港股 独立控制)。"""
    pipeline_pull_a_share: bool | None = None
    pipeline_pull_etf: bool | None = None
    pipeline_pull_index: bool | None = None
    pipeline_pull_hk: bool | None = None


@router.put("/preferences/pipeline-pull-types")
def update_pipeline_pull_types(req: PipelinePullTypesIn) -> dict:
    """更新盘后管道拉取内容开关。"""
    from app.services import preferences
    cfg = req.model_dump(exclude_none=True)
    return preferences.set_pipeline_pull_types(cfg)


class PipelineIndexSymbolsIn(BaseModel):
    """指数自定义拉取代码(逗号/换行/空格分隔,空串表示全量)。"""
    symbols: str = ""


@router.put("/preferences/pipeline-index-symbols")
def update_pipeline_index_symbols(req: PipelineIndexSymbolsIn) -> dict:
    """保存指数自定义拉取代码。"""
    from app.services import preferences
    symbols = preferences.set_pipeline_index_symbols(req.symbols)
    return {"pipeline_index_symbols": symbols}


class QuoteIntervalIn(BaseModel):
    interval: float


class SystemNotifyPrefsIn(BaseModel):
    enabled: bool


@router.put("/preferences/system-notify")
def update_system_notify(req: SystemNotifyPrefsIn) -> dict:
    """系统通知开关 — 开启后监控告警同时推送到操作系统通知中心。

    纯偏好, 无副作用 (不像策略监控要迁移规则), 直接落盘即可。
    quote_service 在每轮告警评估时读此开关决定是否发系统通知。
    """
    from app.services import preferences
    saved = preferences.set_system_notify_enabled(req.enabled)
    return {"system_notify_enabled": saved}


class FeishuWebhookPrefsIn(BaseModel):
    url: str
    secret: str = ""


class WebhookChannelPrefsIn(BaseModel):
    channel: str
    url: str = ""
    secret: str = ""
    nickname: str = ""
    token: str = ""
    clear_token: bool = False


@router.put("/preferences/feishu-webhook")
def update_feishu_webhook(req: FeishuWebhookPrefsIn) -> dict:
    """飞书 Webhook 地址 + 签名密钥 — 全局一处配置, 所有启用推送的监控规则共用。

    - url: 传入空串表示清空配置; 非空则需为合法的飞书自定义机器人地址。
    - secret: 机器人启用了「签名校验」时填密钥, 留空表示不验签。
    """
    from app.services import preferences
    from app.services import webhook_adapter

    url = (req.url or "").strip()
    if url and not webhook_adapter.is_valid_feishu_url(url):
        raise HTTPException(
            status_code=400,
            detail="Webhook 地址非法, 需为飞书自定义机器人地址 "
                   "(https://open.feishu.cn/open-apis/bot/v2/hook/...)",
        )
    saved_url = preferences.set_feishu_webhook_url(url)
    saved_secret = preferences.set_feishu_webhook_secret((req.secret or "").strip())
    return {"feishu_webhook_url": saved_url, "feishu_webhook_secret": saved_secret}


@router.put("/preferences/webhook-channel")
def update_webhook_channel(req: WebhookChannelPrefsIn) -> dict:
    """保存一个 Webhook 通道配置。"""
    from app.services import preferences
    from app.services import webhook_adapter

    channel = (req.channel or "").strip().lower()
    url = (req.url or "").strip()
    if channel == "feishu" and url and not webhook_adapter.is_valid_feishu_url(url):
        raise HTTPException(status_code=400, detail="飞书 Webhook 地址非法")
    if channel == "dingtalk" and url and not webhook_adapter.is_valid_dingtalk_url(url):
        raise HTTPException(status_code=400, detail="钉钉 Webhook 地址非法")
    if channel == "wecom" and url and not webhook_adapter.is_valid_wecom_url(url):
        raise HTTPException(status_code=400, detail="企微 Webhook 地址非法")
    if channel == "meow" and not (req.nickname or "").strip() and url:
        raise HTTPException(status_code=400, detail="MeoW 需填写昵称")
    try:
        saved = preferences.set_webhook_channel(channel, req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"channel": channel, "config": saved, "webhook_channels": preferences.get_webhook_channels()}


class WebhookEnabledDefaultIn(BaseModel):
    enabled: bool


@router.put("/preferences/webhook-enabled-default")
def update_webhook_enabled_default(req: WebhookEnabledDefaultIn) -> dict:
    """新建监控规则时是否默认勾选「飞书推送」。

    数据模型当前只有飞书一个可用渠道 (QMT/ptrade 待定),故此处仅一个布尔。
    单条规则仍可在规则编辑页独立修改此项。
    """
    from app.services import preferences

    saved = preferences.set_webhook_enabled_default(req.enabled)
    return {"webhook_enabled_default": saved}


@router.put("/preferences/quote-interval")
def update_quote_interval(req: QuoteIntervalIn, request: Request) -> dict:
    """更新行情轮询间隔。按档位自动 clamp。"""
    qs = getattr(request.app.state, "quote_service", None)
    if not qs:
        return {"interval": req.interval, "min_interval": qs.get_min_interval(), "max_interval": 60.0}
    clamped = qs.set_interval(req.interval)
    return {
        "interval": clamped,
        "min_interval": qs.get_min_interval(),
        "max_interval": qs.MAX_INTERVAL,
    }


@router.get("/preferences/quote-interval")
def get_quote_interval(request: Request) -> dict:
    """获取当前行情轮询间隔和档位限制。"""
    qs = getattr(request.app.state, "quote_service", None)
    if not qs:
        return {"interval": 10.0, "min_interval": 5.0, "max_interval": 60.0}
    return {
        "interval": qs._interval,
        "min_interval": qs.get_min_interval(),
        "max_interval": qs.MAX_INTERVAL,
    }


class PipelineScheduleIn(BaseModel):
    hour: int
    minute: int


@router.put("/preferences/pipeline-schedule")
def update_pipeline_schedule(req: PipelineScheduleIn, request: Request) -> dict:
    """保存盘后管道调度时间并立即 reschedule。"""
    from app.services import preferences
    sched = preferences.set_pipeline_schedule(req.hour, req.minute)

    # 动态 reschedule
    from apscheduler.triggers.cron import CronTrigger
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        scheduler.reschedule_job(
            "daily_pipeline",
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=sched["hour"],
                minute=sched["minute"],
                timezone="Asia/Shanghai",
            ),
        )
        logger.info("pipeline rescheduled to %02d:%02d mon-fri", sched["hour"], sched["minute"])

    return sched


@router.put("/preferences/instruments-schedule")
def update_instruments_schedule(req: PipelineScheduleIn, request: Request) -> dict:
    """保存盘前标的维表调度时间并立即 reschedule。"""
    from app.services import preferences
    sched = preferences.set_instruments_schedule(req.hour, req.minute)

    from apscheduler.triggers.cron import CronTrigger
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        scheduler.reschedule_job(
            "pre_market_instruments",
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=sched["hour"],
                minute=sched["minute"],
                timezone="Asia/Shanghai",
            ),
        )
        return sched


class EnrichedBatchSizeIn(BaseModel):
    size: int


@router.put("/preferences/enriched-batch-size")
def update_enriched_batch_size(req: EnrichedBatchSizeIn) -> dict:
    """保存 enriched 全量计算批次大小。"""
    from app.services import preferences
    size = preferences.set_enriched_batch_size(req.size)
    return {"enriched_batch_size": size}


class IndexDailyBatchSizeIn(BaseModel):
    size: int


@router.put("/preferences/index-daily-batch-size")
def update_index_daily_batch_size(req: IndexDailyBatchSizeIn) -> dict:
    """保存指数日 K 同步批次大小。"""
    from app.services import preferences
    size = preferences.set_index_daily_batch_size(req.size)
    return {"index_daily_batch_size": size}


# ── 五档盘口 sealed 配置 ──────────────────────────────

class LimitLadderMonitorIn(BaseModel):
    enabled: bool


@router.put("/preferences/limit-ladder-monitor")
def update_limit_ladder_monitor(req: LimitLadderMonitorIn, request: Request) -> dict:
    """连板梯队 5 档监控开关。开启→启动 depth 轮询, 关闭→停止。"""
    from app.services import preferences
    preferences.save({"limit_ladder_monitor_enabled": req.enabled})

    # 立即应用: 启停 depth 轮询线程
    depth_svc = getattr(request.app.state, "depth_service", None)
    if depth_svc:
        depth_svc.apply_monitor_toggle(req.enabled)

    return {"limit_ladder_monitor_enabled": req.enabled}


@router.post("/preferences/limit-ladder-monitor/run")
def run_limit_ladder_fix(request: Request) -> dict:
    """立即手动修正一次真假板(拉取五档盘口 + 更新缓存)。需 depth5.batch。"""
    from app.capabilities import Cap
    capset = request.app.state.capabilities
    capset.require(Cap.DEPTH5_BATCH)  # 无能力抛 CapabilityDenied(403)

    depth_svc = getattr(request.app.state, "depth_service", None)
    if not depth_svc:
        raise HTTPException(status_code=503, detail="depth 服务未初始化")
    return depth_svc.run_once()


class DepthPollingIntervalIn(BaseModel):
    interval: float


@router.put("/preferences/depth-polling-interval")
def update_depth_polling_interval(req: DepthPollingIntervalIn, request: Request) -> dict:
    """保存五档盘口盘中轮询间隔(秒)。需 depth5.batch。"""
    from app.capabilities import Cap
    request.app.state.capabilities.require(Cap.DEPTH5_BATCH)

    from app.services import preferences
    interval = preferences.set_depth_polling_interval(req.interval)
    return {"depth_polling_interval": interval}


class DepthFinalizeTimeIn(BaseModel):
    hour: int
    minute: int


@router.put("/preferences/depth-finalize-time")
def update_depth_finalize_time(req: DepthFinalizeTimeIn, request: Request) -> dict:
    """保存盘后 sealed 定版时间(范围15:01~18:00)并立即 reschedule。需 depth5.batch。"""
    from app.capabilities import Cap
    request.app.state.capabilities.require(Cap.DEPTH5_BATCH)

    from app.services import preferences
    sched = preferences.set_depth_finalize_time(req.hour, req.minute)

    from apscheduler.triggers.cron import CronTrigger
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        scheduler.reschedule_job(
            "depth_finalize",
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=sched["hour"],
                minute=sched["minute"],
                timezone="Asia/Shanghai",
            ),
        )
        logger.info("depth_finalize rescheduled to %02d:%02d mon-fri", sched["hour"], sched["minute"])

    return sched


class ReviewScheduleIn(BaseModel):
    enabled: bool
    hour: int
    minute: int


@router.put("/preferences/review-schedule")
def update_review_schedule(req: ReviewScheduleIn, request: Request) -> dict:
    """保存定时复盘调度并立即更新 APScheduler job。

    - enabled=True: 注册/更新 job(工作日定时生成复盘报告)
    - enabled=False: 移除 job(停止定时复盘)
    - 校验: 开启时若 AI Key 未配置则拒绝(复盘依赖 AI), 提示用户先配置。
    - 时间下限 15:00(A股收盘), 由 preferences 层强制。
    """
    from app.services import preferences

    if req.enabled:
        # 复盘必须有 AI Key, 否则每日报错刷日志
        from app import secrets_store
        if not secrets_store.get_ai_key():
            raise HTTPException(
                status_code=400,
                detail="复盘依赖 AI,请先在「设置 → AI」配置 API Key 后再开启定时复盘",
            )

    sched = preferences.set_review_schedule(req.enabled, req.hour, req.minute)

    # 动态操作 APScheduler job
    from app.jobs.daily_pipeline import _register_review_job, REVIEW_JOB_ID
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        if sched["enabled"]:
            _register_review_job(scheduler, request.app.state.repo, sched["hour"], sched["minute"])
            logger.info("scheduled_review enabled @%02d:%02d mon-fri", sched["hour"], sched["minute"])
        else:
            try:
                scheduler.remove_job(REVIEW_JOB_ID)
                logger.info("scheduled_review disabled (job removed)")
            except Exception:
                pass  # job 本就不存在(从未开过), 无需处理

    return sched


class ReviewPushIn(BaseModel):
    channels: list[str]  # 多选: ['feishu'] 等; 空数组=不推送。微信等开发中


@router.put("/preferences/review-push")
def update_review_push(req: ReviewPushIn) -> dict:
    """复盘推送渠道(多选) — 选定把复盘报告(手动生成 / 定时生成归档后)推送到哪些外部工具。

    纯偏好, 与定时复盘 / 实时行情完全独立, 常驻可单独设置。空数组=不推送。
    实际推送由归档端点(POST /api/market-recap/reports)与定时任务(_run_scheduled_review)
    在归档后读取本列表逐个推送。白名单外的渠道会被过滤掉。
    """
    from app.services import preferences
    saved = preferences.set_review_push_channels(req.channels)
    return {"review_push_channels": saved}


class TradingAutoReviewIn(BaseModel):
    tradingAutoReview: bool


@router.put("/preferences/trading-auto-review")
def update_trading_auto_review(req: TradingAutoReviewIn) -> dict:
    """保存交易自动复盘开关 (P6.4 盘后状态驱动 AI 归因)。

    纯偏好写入; APScheduler job 在 start_scheduler 启动时注册, 到点读此开关决定
    是否执行实质逻辑 (false=零开销直接返回)。手动触发走 POST /api/trading/review/auto-run。
    """
    from app.services import preferences
    saved = preferences.set_trading_auto_review(req.tradingAutoReview)
    return {"tradingAutoReview": saved}


class StructuredPlanCheckIn(BaseModel):
    enabled: bool


@router.put("/preferences/structured-plan-check")
def update_structured_plan_check(req: StructuredPlanCheckIn) -> dict:
    """保存结构化计划检查开关 (P4 默认关闭)。

    纯偏好写入。关闭时计划检查端点返回 HTTP 403、零 AI 调用。
    """
    from app.services import preferences
    saved = preferences.set_structured_plan_check_enabled(req.enabled)
    return {"structured_plan_check_enabled": saved}


class ExternalFallbackPrefsIn(BaseModel):
    external_fallback_enabled: bool = False
    external_fallback_scopes: list[str] = []


@router.put("/preferences/external-fallback")
def update_external_fallback(req: ExternalFallbackPrefsIn) -> dict:
    """保存受控外部 fallback 偏好 (P1 realtime, 默认关闭)。

    仅 realtime/depth scope 白名单内合法; 非 scope 直接 400, 不做静默过滤。
    返回清洗后的偏好 {external_fallback_enabled, external_fallback_scopes}。
    启用不触发任何网络; fallback 仅在本地 realtime 快照缺失/陈旧且为交易日时触发。
    """
    from app.services import preferences
    try:
        enabled, scopes = preferences.set_external_fallback(
            req.external_fallback_enabled, req.external_fallback_scopes
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"external_fallback_enabled": enabled, "external_fallback_scopes": scopes}
