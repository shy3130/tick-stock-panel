"""设置 API — Key 配置 / 模式切换。

提供面向非开发者的 UI 配置入口,避免逼用户改 .env。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import secrets_store
from app.tickflow import client as tf_client
from app.tickflow.policy import (
    detect_capabilities,
    extras_caps,
    missing_caps,
    probe_log,
    tier_label,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# 默认端点 —— endpoints.json 列表第一项,UI"当前使用"始终对齐此项。
# 注意:Free 模式 SDK 实际走 free-api(免费数据通道),但 UI 显示统一用默认节点。
DEFAULT_PAID_ENDPOINT = "https://api.tickflow.org"


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


class TickflowKeyIn(BaseModel):
    api_key: str


@router.get("")
def get_settings() -> dict:
    """返回当前配置概况(Key 脱敏)。"""
    from app.config import settings
    from app.services import preferences
    from app.services.ai_provider import (
        ai_configured,
        current_ai_model,
        current_codex_command,
        current_codex_reasoning_effort,
    )

    key = secrets_store.get_tickflow_key()
    ai_provider = secrets_store.get_ai_config("ai_provider", settings.ai_provider)
    return {
        "mode": tf_client.current_mode(),
        "tickflow_api_key_masked": secrets_store.mask(key),
        "has_tickflow_key": bool(key),
        "tier_label": tier_label(),
        "current_endpoint": tf_client.current_endpoint(),
        "probe_log": probe_log(),
        "missing_caps": missing_caps(),
        "extras_caps": extras_caps(),
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
        "ai_codex_reasoning_effort": current_codex_reasoning_effort(),
        "ai_user_agent": secrets_store.get_ai_config("ai_user_agent", settings.ai_user_agent),
    }


class SwitchEndpointIn(BaseModel):
    url: str


@router.post("/switch_endpoint")
def switch_endpoint(req: SwitchEndpointIn, request: Request) -> dict:
    """切换 TickFlow 端点并立即生效。

    端点切换仅对付费档(starter+,走 api.tickflow.org)有意义;
    none/free 档运行在 free-api 服务器,无付费端点权限,禁止切换。
    """
    # none/free 档没有付费端点权限,禁止切换
    if tf_client.current_mode() != "api_key":
        return {"ok": False, "error": "当前档位无法切换端点,仅付费套餐(Starter+)支持"}

    url = req.url.strip().rstrip("/")
    if not url.startswith("https://"):
        return {"ok": False, "error": "仅支持 HTTPS 端点"}

    # 持久化到 secrets.json
    secrets_store.save({"tickflow_base_url": url})
    # 重置客户端，下次调用自动用新端点
    tf_client.reset_clients()

    return {
        "ok": True,
        "current_endpoint": tf_client.current_endpoint(),
    }


@router.post("/tickflow-key")
def save_tickflow_key(req: TickflowKeyIn, request: Request) -> dict:
    """保存 TickFlow API Key 并立即重新探测能力。

    先探后存(关键改动,修复乱填 key 也会被持久化的问题):
      1. 临时用新 key 探测(付费端点),判定档位
      2. 判定为 none(连单只日K都拿不到)→ key 无效:不存,清除已存的,
         返回 {ok: false, reason: "invalid"},前端提示「Key 无效」
      3. 判定为 free(免费有效 key)→ 存 key,客户端切到 free-api 服务器
      4. 判定为 starter+ → 存 key,切到付费端点(现有逻辑)

    端点联动:从无 key 升级到付费 key 时,残留的 free-api 端点不可用,
    故自动切到默认付费端点(api.tickflow.org);free 档则清除自定义端点。
    """
    from app.tickflow.policy import (
        base_tier_name, is_invalid_key,
    )

    key = req.api_key.strip()
    if not key:
        return {"ok": False, "error": "key empty"}

    # ===== 1) 临时存 key + 重置客户端,让探测走付费端点 =====
    secrets_store.save({"tickflow_api_key": key})
    tf_client.reset_clients()

    # 立即重新探测(此时 client 已按档位判定,但首次探测必然走付费端点验证)
    capset = detect_capabilities(force=True)
    request.app.state.capabilities = capset
    _sync_financial_scheduler_caps(request.app.state, capset)

    # ===== 2) 判定为无效 key(连单只日K都拿不到)→ 不存,清除 =====
    if is_invalid_key() or base_tier_name() == "none":
        # 无效 key:清除刚存的,避免乱填被持久化;退回 none 档
        secrets_store.clear("tickflow_api_key", "tickflow_base_url")
        tf_client.reset_clients()
        capset = detect_capabilities(force=True)
        request.app.state.capabilities = capset
        _sync_financial_scheduler_caps(request.app.state, capset)
        return {
            "ok": False,
            "reason": "invalid",
            "error": "Key 无效或已过期,请检查后重试",
            "mode": "none",
            "tier_label": tier_label(),
            "current_endpoint": tf_client.current_endpoint(),
            "probe_log": [],
            "capabilities_count": len(capset.all()),
        }

    # ===== 3) free 档(免费有效 key)→ 存 key,切到 free-api 服务器 =====
    if base_tier_name() == "free":
        # 免费档运行时走 free-api 服务器,清除付费端点的自定义配置
        secrets_store.clear("tickflow_base_url")
        tf_client.reset_clients()
        return {
            "ok": True,
            "tickflow_api_key_masked": secrets_store.mask(key),
            "mode": "free",
            "tier_label": tier_label(),
            "current_endpoint": tf_client.current_endpoint(),
            "probe_log": [],
            "capabilities_count": len(capset.all()),
        }

    # ===== 4) starter+ 付费档 → 确保走付费端点(现有逻辑) =====
    # 若之前是 none/free(无自定义付费端点),切到默认付费端点
    base = secrets_store.load().get("tickflow_base_url")
    if not base:
        secrets_store.save({"tickflow_base_url": DEFAULT_PAID_ENDPOINT})
    tf_client.reset_clients()

    return {
        "ok": True,
        "tickflow_api_key_masked": secrets_store.mask(key),
        "mode": "api_key",
        "tier_label": tier_label(),
        "current_endpoint": tf_client.current_endpoint(),
        "probe_log": [],
        "capabilities_count": len(capset.all()),
    }


@router.delete("/tickflow-key")
def clear_tickflow_key(request: Request) -> dict:
    """清除 Key,退回无档(none)。

    同时清除 tickflow_base_url(测速切换的自定义端点),使客户端走 free-api
    服务器取历史日K;档位标签为 None(无档)。
    """
    secrets_store.clear("tickflow_api_key", "tickflow_base_url")
    tf_client.reset_clients()

    capset = detect_capabilities(force=True)
    request.app.state.capabilities = capset
    _sync_financial_scheduler_caps(request.app.state, capset)

    return {
        "ok": True,
        "mode": "none",
        "tier_label": tier_label(),
        "current_endpoint": tf_client.current_endpoint(),
        "capabilities_count": len(capset.all()),
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
    codex_reasoning_effort: str = ""
    user_agent: str = ""


@router.post("/ai")
def save_ai_settings(req: AiSettingsIn) -> dict:
    """保存 AI 配置（全部持久化到 secrets.json）"""
    from app.config import settings
    from app.services.ai_provider import (
        ai_configured,
        current_ai_model,
        current_ai_provider,
        current_codex_command,
        current_codex_reasoning_effort,
        normalize_codex_command,
        normalize_codex_reasoning_effort,
    )

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
        codex_reasoning_effort = normalize_codex_reasoning_effort(req.codex_reasoning_effort)
        updates["ai_codex_command"] = codex_command
        updates["ai_codex_reasoning_effort"] = codex_reasoning_effort
        settings.ai_codex_command = codex_command
        settings.ai_codex_reasoning_effort = codex_reasoning_effort
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
        "ai_codex_reasoning_effort": current_codex_reasoning_effort(),
        "ai_configured": ai_configured(provider),
    }


@router.delete("/ai")
def clear_ai_settings() -> dict:
    """一键清空 AI 配置(provider / base_url / api_key / model)。

    保留 ai_user_agent —— 自定义请求头与凭证解耦,清空凭证不影响绕过 CDN 拦截的设置。
    """
    from app.config import settings

    secrets_store.clear("ai_provider", "ai_base_url", "ai_api_key", "ai_model", "ai_codex_command", "ai_codex_reasoning_effort")
    # 同步重置运行时内存(provider 回默认值,其余置空)
    settings.ai_provider = "openai_compat"
    settings.ai_base_url = ""
    settings.ai_api_key = ""
    settings.ai_model = ""
    settings.ai_codex_command = "codex"
    settings.ai_codex_reasoning_effort = ""

    return {"ok": True}


# ===== 偏好设置 =====

def _realtime_allowed() -> bool:
    """当前档位是否允许实时行情(none/free 不允许)。"""
    from app.services.quote_service import QuoteService
    return QuoteService.is_realtime_allowed()


class MinuteSyncPrefs(BaseModel):
    minute_sync_enabled: bool
    minute_sync_days: int = 5
    # 单段大小(交易日),None 表示不修改现有值。范围 [5, 30],默认 20。
    minute_sync_segment_days: int | None = None


class DataProvidersIn(BaseModel):
    daily_data_provider: str | None = None
    adj_factor_provider: str | None = None
    minute_data_provider: str | None = None
    realtime_data_provider: str | None = None
    financial_data_provider: str | None = None


class CustomSourceTestIn(BaseModel):
    provider: str
    dataset: str
    symbols: list[str] | None = None


class DatasetFieldMapItem(BaseModel):
    source: str
    target: str


class DatasetConfigIn(BaseModel):
    url: str
    method: str = "GET"
    batch: int | None = None
    rpm: int | None = None
    response_path: str = ""
    field_map: dict[str, str] = {}
    transforms: dict[str, str] = {}
    symbols_param: str = "symbols"
    start_param: str = "start_time"
    end_param: str = "end_time"


class AuthConfigIn(BaseModel):
    type: str = "none"
    token_env: str | None = None
    header: str = "Authorization"
    param: str = "token"


class CustomSourceIn(BaseModel):
    name: str
    display_name: str = ""
    auth: AuthConfigIn = AuthConfigIn()
    datasets: dict[str, DatasetConfigIn] = {}


@router.get("/preferences")
def get_preferences() -> dict:
    """返回用户偏好设置。"""
    from app.services import preferences
    return {
        "realtime_quotes_enabled": preferences.get_realtime_quotes_enabled(),
        "realtime_allowed": _realtime_allowed(),
        "indices_nav_pinned": preferences.get_indices_nav_pinned(),
        "minute_sync_enabled": preferences.get_minute_sync_enabled(),
        "minute_sync_days": preferences.get_minute_sync_days(),
        "minute_sync_segment_days": preferences.get_minute_sync_segment_days(),
        "daily_data_provider": preferences.get_daily_data_provider(),
        "adj_factor_provider": preferences.get_adj_factor_provider(),
        "minute_data_provider": preferences.get_minute_data_provider(),
        "realtime_data_provider": preferences.get_realtime_data_provider(),
        "financial_data_provider": preferences.get_financial_provider(),
        "realtime_watchlist_symbols": preferences.get_realtime_watchlist_symbols(),
        **preferences.get_realtime_quote_scope(),
        "pipeline_pull_a_share": preferences.get_pipeline_pull_a_share(),
        "pipeline_pull_etf": preferences.get_pipeline_pull_etf(),
        "pipeline_pull_index": preferences.get_pipeline_pull_index(),
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
        "wecom_webhook_url": preferences.get_wecom_webhook_url(),
        "wecom_bot_id": preferences.get_wecom_bot_id(),
        "wecom_bot_secret": preferences.get_wecom_bot_secret(),
        "wecom_bot_enabled": preferences.get_wecom_bot_enabled(),
        "webhook_enabled_default": preferences.get_webhook_enabled_default(),
        "webhook_default_channels": preferences.get_webhook_default_channels(),
        "sidebar_index_symbols": preferences.get_sidebar_index_symbols(),
        "minute_intraday_refresh": preferences.get_minute_intraday_refresh(),
        "minute_intraday_refresh_interval": preferences.get_minute_intraday_refresh_interval(),
        "monitor_ext_fields": preferences.get_monitor_ext_fields(),
        "nav_order": preferences.get_nav_order(),
        "nav_hidden": preferences.get_nav_hidden(),
        "screener_auto_run": preferences.get_screener_auto_run(),
        "limit_ladder_monitor_enabled": preferences.get_limit_ladder_monitor_enabled(),
        "depth_polling_interval": preferences.get_depth_polling_interval(),
        "depth_finalize_time": preferences.get_depth_finalize_time(),
        "review_schedule": preferences.get_review_schedule(),
        "review_push_channels": preferences.get_review_push_channels(),
    }


@router.get("/data-sources")
def list_data_sources() -> dict:
    """列出已加载的数据源 (内置 / 插件 / 用户自定义)。"""
    from app.data_providers import custom as custom_sources
    return {
        "builtin": [{"name": "tickflow", "display_name": "TickFlow", "datasets": ["daily", "adj_factor", "realtime", "minute"]}],
        "plugins": custom_sources.list_plugins(),
        "custom": custom_sources.list_sources(),
        "errors": custom_sources.errors(),
        "config_dir": str(custom_sources.data_sources_dir()),
    }


@router.post("/data-sources/reload")
def reload_data_sources() -> dict:
    """重新加载 data_sources/*.yaml。"""
    from app.data_providers import custom as custom_sources
    custom_sources.load_all()
    return list_data_sources()


@router.post("/plugins/{name}/install")
def install_plugin(name: str) -> dict:
    """安装指定插件的依赖 (npm install / pip install), 完成后重新扫描。

    根据 plugin.yaml 的 runtime 字段决定安装方式。安装可能耗时较长 (网络下载),
    客户端需设较长超时。
    """
    from app.data_providers import custom as custom_sources
    if not custom_sources.is_builtin(name):
        raise HTTPException(status_code=404, detail=f"插件 '{name}' 不存在")
    ok, message = custom_sources.install_plugin(name)
    # 无论成功失败都重新扫描, 刷新插件状态 (安装可能部分成功)
    custom_sources.load_all()
    result = list_data_sources()
    result["install_ok"] = ok
    result["install_message"] = message
    return result


@router.delete("/plugins/{name}/install")
def uninstall_plugin(name: str) -> dict:
    """卸载指定插件的依赖 (删除 node_modules / pip uninstall), 完成后重新扫描。

    如果该插件当前正被使用, 自动回退到 tickflow。
    """
    from app.data_providers import custom as custom_sources
    from app.services import preferences
    if not custom_sources.is_builtin(name):
        raise HTTPException(status_code=404, detail=f"插件 '{name}' 不存在")
    ok, message = custom_sources.uninstall_plugin(name)
    # 卸载后若该插件正被使用, 回退 tickflow
    for getter, key, default in [
        (preferences.get_daily_data_provider, "daily_data_provider", "tickflow"),
        (preferences.get_minute_data_provider, "minute_data_provider", "tickflow"),
        (preferences.get_realtime_data_provider, "realtime_data_provider", "tickflow"),
        (preferences.get_financial_provider, "financial_data_provider", "tickflow"),
    ]:
        if getter() == name:
            preferences.save({key: default})
    custom_sources.load_all()
    result = list_data_sources()
    result["uninstall_ok"] = ok
    result["uninstall_message"] = message
    return result


@router.get("/data-sources/{name}")
def get_data_source(name: str) -> dict:
    """读取一个自定义数据源的完整配置(用于前端编辑回填)。"""
    from app.data_providers import custom as custom_sources
    cfg = custom_sources.get_config_dict(name)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"数据源 '{name}' 不存在")
    return cfg


@router.post("/data-sources")
def save_data_source(req: CustomSourceIn) -> dict:
    """创建或更新一个自定义数据源 yaml, 保存后自动 reload。"""
    from app.data_providers import custom as custom_sources
    config = req.model_dump()
    config["name"] = (config.get("name") or "").lower()
    try:
        custom_sources.save_config(config["name"], config)
        custom_sources.load_all()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return list_data_sources()


@router.delete("/data-sources/{name}")
def delete_data_source(name: str) -> dict:
    """删除一个自定义数据源 yaml, 保存后自动 reload。

    若当前总开关选中的就是被删的源, 回退到 tickflow。
    """
    from app.data_providers import custom as custom_sources
    from app.services import preferences
    try:
        custom_sources.delete_config(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    custom_sources.load_all()
    # 回退被删源的偏好
    updates: dict = {}
    if preferences.get_daily_data_provider() == name:
        updates["daily_data_provider"] = "tickflow"
    if preferences.get_realtime_data_provider() == name:
        updates["realtime_data_provider"] = "tickflow"
    if preferences.get_financial_provider() == name:
        updates["financial_data_provider"] = "tickflow"
    adj = preferences.get_adj_factor_provider()
    if adj == name:
        updates["adj_factor_provider"] = "same_as_daily"
    if updates:
        preferences.save(updates)
    return list_data_sources()


@router.post("/data-sources/test")
def test_data_source(req: CustomSourceTestIn) -> dict:
    """试拉自定义数据源，不写盘。"""
    from app.data_providers import custom as custom_sources
    provider = custom_sources.get_provider(req.provider)
    try:
        return provider.test_dataset(req.dataset, req.symbols)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"自定义数据源测试失败: {e}") from e


@router.put("/preferences/data-providers")
def update_data_providers(req: DataProvidersIn) -> dict:
    """保存数据源选择。"""
    from app.services import preferences
    updates = req.model_dump(exclude_none=True)
    if updates:
        preferences.save(updates)
    return {
        "daily_data_provider": preferences.get_daily_data_provider(),
        "adj_factor_provider": preferences.get_adj_factor_provider(),
        "minute_data_provider": preferences.get_minute_data_provider(),
        "realtime_data_provider": preferences.get_realtime_data_provider(),
        "financial_data_provider": preferences.get_financial_provider(),
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
    """保存分钟 K 同步偏好。

    minute_sync_segment_days 为可选:未传(None)时不覆盖现有值,便于开关/天数
    与段大小各自独立更新。
    """
    from app.services import preferences
    days = max(1, min(30, req.minute_sync_days))
    updates: dict = {
        "minute_sync_enabled": req.minute_sync_enabled,
        "minute_sync_days": days,
    }
    if req.minute_sync_segment_days is not None:
        updates["minute_sync_segment_days"] = max(5, min(30, req.minute_sync_segment_days))
    preferences.save(updates)
    return {
        "minute_sync_enabled": req.minute_sync_enabled,
        "minute_sync_days": days,
        "minute_sync_segment_days": preferences.get_minute_sync_segment_days(),
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

    none 档无实时行情权限；free 档开启自选股实时；starter+ 开启全市场实时。
    前端据此把开关置灰 / 回弹。
    """
    from app.services import preferences
    qs = getattr(request.app.state, "quote_service", None)

    allowed = qs.is_realtime_allowed() if qs else True
    if req.realtime_quotes_enabled and not allowed:
        # 当前档位不允许开启实时行情 — 强制关闭
        preferences.save({"realtime_quotes_enabled": False})
        if qs:
            qs.disable()
        return {"realtime_quotes_enabled": False, "realtime_allowed": False}
    if req.realtime_quotes_enabled and qs and qs.is_paused():
        # 管道/数据修正运行期间禁止开启实时行情 — 防止写盘竞态
        raise HTTPException(status_code=409, detail="数据同步运行中，实时行情已临时暂停，请稍后再开启")
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
    minute_intraday_refresh: bool | None = None
    minute_intraday_refresh_interval: int | None = None
    monitor_ext_fields: dict | None = None


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
    """盘后管道拉取内容开关(A股 / ETF / 指数 独立控制)。"""
    pipeline_pull_a_share: bool | None = None
    pipeline_pull_etf: bool | None = None
    pipeline_pull_index: bool | None = None


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


class WecomWebhookPrefsIn(BaseModel):
    url: str


@router.put("/preferences/wecom-webhook")
def update_wecom_webhook(req: WecomWebhookPrefsIn) -> dict:
    """企业微信群推送 Webhook 地址 — 与飞书并列的第二推送通道。

    - url: 传入空串表示清空配置; 非空需为合法企业微信群推送 Webhook 地址, 或纯 key。
    - 用户可只填 key (webhook/send?key=xxx 的 xxx 部分), 后端自动补全为完整 URL。
    """
    from app.services import preferences
    from app.services import webhook_adapter

    url = (req.url or "").strip()
    if url and not webhook_adapter.is_valid_wecom_url(url):
        raise HTTPException(
            status_code=400,
            detail="Webhook 地址非法, 需为企业微信群推送 Webhook 地址 "
                   "(https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=... 或纯 key)",
        )
    saved_url = preferences.set_wecom_webhook_url(url)
    return {"wecom_webhook_url": saved_url}


class WecomBotPrefsIn(BaseModel):
    bot_id: str
    secret: str
    enabled: bool = True


@router.put("/preferences/wecom-bot")
def update_wecom_bot(req: WecomBotPrefsIn, request: Request) -> dict:
    """企业微信智能机器人(BotID + Secret)配置 — 长连接通道。

    保存凭证后立即重建连接(stop→start), 因每机器人仅允许 1 条长连接。
    - bot_id/secret 均传空串表示清空配置并断开连接。
    - enabled 控制是否启用长连接(凭证齐全时生效)。
    """
    from app.services import preferences

    bot_id = (req.bot_id or "").strip()
    secret = (req.secret or "").strip()
    preferences.set_wecom_bot_id(bot_id)
    preferences.set_wecom_bot_secret(secret)
    # 凭证不齐时强制关闭(避免 enabled=True 但连不上)
    enabled = req.enabled and bool(bot_id) and bool(secret)
    preferences.set_wecom_bot_enabled(enabled)

    # 立即应用: 重建连接
    bot_svc = getattr(request.app.state, "wecom_bot_service", None)
    status: dict = {}
    if bot_svc:
        bot_svc.apply_credential_change()
        status = bot_svc.status()
    return {
        "wecom_bot_id": preferences.get_wecom_bot_id(),
        "wecom_bot_secret": preferences.get_wecom_bot_secret(),
        "wecom_bot_enabled": preferences.get_wecom_bot_enabled(),
        "wecom_bot_status": status,
    }


class WecomBotToggleIn(BaseModel):
    enabled: bool


@router.put("/preferences/wecom-bot-toggle")
def toggle_wecom_bot(req: WecomBotToggleIn, request: Request) -> dict:
    """独立开关: 启用/禁用智能机器人长连接(不改动凭证)。

    凭证不齐时强制返回未启用(无法连接)。
    """
    from app.services import preferences

    bot_id = preferences.get_wecom_bot_id()
    secret = preferences.get_wecom_bot_secret()
    enabled = req.enabled and bool(bot_id) and bool(secret)
    preferences.set_wecom_bot_enabled(enabled)

    bot_svc = getattr(request.app.state, "wecom_bot_service", None)
    status: dict = {}
    if bot_svc:
        bot_svc.apply_credential_change()
        status = bot_svc.status()
    return {"wecom_bot_enabled": enabled, "wecom_bot_status": status}


class WebhookEnabledDefaultIn(BaseModel):
    enabled: bool


@router.put("/preferences/webhook-enabled-default")
def update_webhook_enabled_default(req: WebhookEnabledDefaultIn) -> dict:
    """新建监控规则时是否默认勾选推送 (老布尔接口, 兼容旧前端)。

    新数据模型为渠道数组 (webhook_default_channels); 此处转译为
    True→['feishu','wecom'], False→[]。新前端请改用 webhook-default-channels 接口。
    """
    from app.services import preferences

    saved = preferences.set_webhook_enabled_default(req.enabled)
    return {"webhook_enabled_default": saved}


class WebhookDefaultChannelsIn(BaseModel):
    channels: list[str]  # 多选: ['feishu','wecom'] 等; 空数组=默认不推送


@router.put("/preferences/webhook-default-channels")
def update_webhook_default_channels(req: WebhookDefaultChannelsIn) -> dict:
    """新建监控规则时默认勾选的推送渠道 (多选)。

    作为新建规则的默认推送渠道预填, 单条规则仍可在规则编辑页独立修改。
    空数组=默认不推送。白名单外的渠道会被过滤掉。
    """
    from app.services import preferences

    saved = preferences.set_webhook_default_channels(req.channels)
    return {"webhook_default_channels": saved}


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
        return {"interval": 6.0, "min_interval": 6.0, "max_interval": 60.0}
    return {
        "interval": qs._interval,
        "min_interval": qs.get_min_interval(),
        "max_interval": qs.MAX_INTERVAL,
    }


class TestEndpointIn(BaseModel):
    url: str
    # 测试轮数;不传时取 endpoints.json 的 testRounds(默认 5)
    rounds: int | None = None


# 官方端点发现清单 —— 前端浏览器无法直接跨域拉取 tickflow.org/endpoints.json
# (无 CORS 头),因此由后端代理。缓存 5 分钟,失败时回退到内置列表。
ENDPOINTS_URL = "https://tickflow.org/endpoints.json"
ENDPOINTS_TTL = 300.0  # 秒

# 回退列表 —— 与官方 endpoints.json 的 endpoints[] 字段对齐。
# 当远程拉取失败时使用,保证 UI 永远有内容可显示。
_FALLBACK_ENDPOINTS: list[dict] = [
    {
        "id": "default",
        "url": "https://api.tickflow.org",
        "label": "默认端点",
        "region": "auto",
        "description": "默认端点",
        "premium": False,
    },
    {
        "id": "hk",
        "url": "https://hk-api.tickflow.org",
        "label": "香港端点",
        "region": "ap-east-1",
        "description": "备用端点，部分地区访问更稳定",
        "premium": False,
    },
    {
        "id": "sg",
        "url": "https://sg-api.tickflow.org",
        "label": "新加坡端点",
        "region": "ap-southeast-1",
        "description": "备用端点，亚太地区访问更稳定",
        "premium": False,
    },
    {
        "id": "us",
        "url": "https://us-api.tickflow.org",
        "label": "美国端点",
        "region": "us-east-1",
        "description": "备用端点，欧美地区访问更稳定",
        "premium": False,
    },
    {
        "id": "cn",
        "url": "https://139.196.55.234:50443",
        "label": "中国大陆端点（Beta）",
        "region": "cn-east-1",
        "description": "备用端点，中国大陆地区访问更稳定，目前处于测试阶段，谨慎使用",
        "premium": False,
    },
    {
        "id": "cn-premium",
        "url": "https://106.15.238.72:50443",
        "label": "中国大陆专线端点",
        "region": "cn-east-1",
        "description": "专线加速端点，需要专线加速权限（该权限包含在 Expert 及以上套餐中，也可通过自定义组合单独开通）",
        "premium": True,
    },
]

# 进程内缓存:{ "ts": float, "data": dict }
_endpoints_cache: dict = {"ts": 0.0, "data": None}


@router.get("/endpoints")
def list_endpoints() -> dict:
    """代理拉取 tickflow.org/endpoints.json 并返回规范化端点列表。

    前端无法跨域直连该 URL(无 CORS 头),故由本接口代理。带 8s 超时、
    5 分钟内存缓存,远程失败时回退到内置列表,保证 UI 始终有内容。
    返回结构与原始 endpoints.json 一致(透传 schema/version 等元信息)。
    """
    import httpx

    now = time.monotonic()
    cached = _endpoints_cache.get("data")
    if cached is not None and (now - _endpoints_cache["ts"]) < ENDPOINTS_TTL:
        return cached

    source = "remote"
    data: dict | None = None
    try:
        resp = httpx.get(ENDPOINTS_URL, timeout=8.0, follow_redirects=True)
        if resp.status_code == 200:
            parsed = resp.json()
            eps = parsed.get("endpoints")
            # 校验:必须是列表且每项含必要字段,否则视为无效
            if isinstance(eps, list) and all(
                isinstance(e, dict) and "url" in e for e in eps
            ):
                data = {
                    "version": parsed.get("version", 1),
                    "description": parsed.get(
                        "description", "TickFlow API 端点配置"
                    ),
                    "healthPath": parsed.get("healthPath", "/health"),
                    "testRounds": parsed.get("testRounds", 5),
                    "endpoints": eps,
                }
    except (httpx.HTTPError, ValueError):
        logger.warning("拉取 endpoints.json 失败，使用内置回退列表", exc_info=True)

    if data is None:
        source = "fallback"
        data = {
            "version": 1,
            "description": "TickFlow API 端点配置",
            "healthPath": "/health",
            "testRounds": 5,
            "endpoints": _FALLBACK_ENDPOINTS,
        }

    # 标记数据来源,便于前端提示(回退时显示"内置列表")。
    data["source"] = source
    _endpoints_cache["ts"] = now
    _endpoints_cache["data"] = data
    return data


async def _http_ping(url: str, timeout: float = 10.0) -> float | None:
    """单次异步 GET 请求并返回延迟(ms),失败返回 None。

    对齐官方 latency_test.py:用 /health 轻量端点测真实网络延迟,
    不携带 API Key(/health 公开)。异步实现,保证多端点并行测速不阻塞。
    """
    import httpx

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            dt = (time.perf_counter() - t0) * 1000
            # 只把 <400 视为成功;4xx/5xx 也算"不可达"
            if resp.status_code < 400:
                return round(dt, 2)
            return None
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError, OSError):
        return None


@router.post("/test_endpoint")
async def test_endpoint(req: TestEndpointIn) -> dict:
    """测试端点网络延迟:对 /health 多轮探测取中位数。

    参考 TickFlow 官方 latency_test.py:
    - 路径用 /health(公开、轻量),反映真实网络延迟而非业务接口耗时
    - 多轮探测(默认 5 轮,取自 endpoints.json 的 testRounds),间隔 0.3s
    - 返回 median/min/max/success,前端显示中位数
    - 异步实现,保证"全部测速"时多端点真正并行
    """
    import asyncio
    import statistics

    base = req.url.rstrip("/")
    rounds = max(1, min(10, req.rounds or _endpoints_cache.get("data", {}).get("testRounds", 5)))
    health_url = base + "/health"

    latencies: list[float] = []
    for _ in range(rounds):
        ms = await _http_ping(health_url)
        if ms is not None:
            latencies.append(ms)
        # 官方脚本间隔 0.3s;末轮无需等待
        await asyncio.sleep(0.3)

    success = len(latencies)
    if success == 0:
        return {
            "ok": False,
            "error": "不可达",
            "url": req.url,
            "rounds": rounds,
            "success": 0,
            "median_ms": None,
            "min_ms": None,
            "max_ms": None,
        }

    median = round(statistics.median(latencies), 2)
    return {
        "ok": True,
        "url": req.url,
        "rounds": rounds,
        "success": success,
        "median_ms": median,
        "min_ms": round(min(latencies), 2),
        "max_ms": round(max(latencies), 2),
        # 兼容旧字段:取中位数作为代表延迟
        "latency_ms": median,
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
    """立即手动修正一次真假板(拉取五档盘口 + 更新缓存)。需 Pro+。"""
    from app.tickflow.capabilities import Cap
    capset = request.app.state.capabilities
    capset.require(Cap.DEPTH5_BATCH)  # 无能力抛 CapabilityDenied(403)

    depth_svc = getattr(request.app.state, "depth_service", None)
    if not depth_svc:
        raise HTTPException(status_code=503, detail="depth 服务未初始化")
    result = depth_svc.run_once()
    # sealed 数据变了, 清看板总览缓存, 否则看板在 TTL 窗口内仍返回旧的 limit_up/fake 等
    if result.get("ok"):
        from app.api.overview import invalidate_overview_cache
        invalidate_overview_cache()
    return result


class DepthPollingIntervalIn(BaseModel):
    interval: float


@router.put("/preferences/depth-polling-interval")
def update_depth_polling_interval(req: DepthPollingIntervalIn, request: Request) -> dict:
    """保存五档盘口盘中轮询间隔(秒)。需 Pro+。"""
    from app.tickflow.capabilities import Cap
    request.app.state.capabilities.require(Cap.DEPTH5_BATCH)

    from app.services import preferences
    interval = preferences.set_depth_polling_interval(req.interval)
    return {"depth_polling_interval": interval}


class DepthFinalizeTimeIn(BaseModel):
    hour: int
    minute: int


@router.put("/preferences/depth-finalize-time")
def update_depth_finalize_time(req: DepthFinalizeTimeIn, request: Request) -> dict:
    """保存盘后 sealed 定版时间(范围15:01~18:00)并立即 reschedule。需 Pro+。"""
    from app.tickflow.capabilities import Cap
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

