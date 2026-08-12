"""用户偏好设置持久化。

存储位置: data/user_data/preferences.json
沿用 secrets_store 的 merge-write 模式,但不做 chmod 0600 (非敏感数据)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "preferences.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("preferences.json malformed: %s", e)
    return {}


def save(updates: dict) -> dict:
    """合并写入。返回新内容。"""
    current = load()
    current.update(updates)
    _path().write_text(
        json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return current


def get_realtime_quotes_enabled() -> bool:
    return load().get("realtime_quotes_enabled", False)


def get_indices_nav_pinned() -> bool:
    """侧栏指数报价卡片是否固定显示。默认 True（常驻）。
    关闭后，卡片跟随实时行情开关（仅实时开时显示）。"""
    return load().get("indices_nav_pinned", True)


def get_realtime_quote_interval() -> float:
    return load().get("realtime_quote_interval", 10.0)


def get_realtime_watchlist_symbols() -> list[str]:
    """Free 档自选实时监控标的:直接取自选页前 5 个。"""
    try:
        from app.services import watchlist
        rows = watchlist.list_symbols()
    except Exception as e:  # noqa: BLE001
        logger.warning("load watchlist for realtime failed: %s", e)
        return []
    out: list[str] = []
    for row in rows:
        symbol = str((row or {}).get("symbol") or "").strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
        if len(out) >= 5:
            break
    return out




def set_realtime_quote_interval(interval: float) -> float:
    """保存行情轮询间隔（不在此做 min/max 校验，由调用方按档位限制）。"""
    current = load()
    current["realtime_quote_interval"] = interval
    _path().write_text(
        json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return interval


def get_minute_sync_enabled() -> bool:
    return load().get("minute_sync_enabled", False)


def get_minute_sync_days() -> int:
    return max(1, min(30, load().get("minute_sync_days", 5)))


# ===== 数据源选择 =====

_ALLOWED_DATA_PROVIDERS = {"fquant", "fquant_local"}


def _clean_data_provider(provider: str | None, default: str = "fquant_local") -> str:
    provider = str(provider or default).strip().lower() or default
    return provider if provider in _ALLOWED_DATA_PROVIDERS else "fquant_local"


def get_data_provider() -> str:
    return _clean_data_provider(load().get("data_provider"))


def set_data_provider(provider: str) -> str:
    provider = str(provider or "").strip().lower()
    if provider not in _ALLOWED_DATA_PROVIDERS:
        raise ValueError(f"unsupported data provider: {provider}")
    save({
        "data_provider": provider,
        "daily_data_provider": provider,
        "minute_data_provider": provider,
        "realtime_data_provider": provider,
        "financial_data_provider": provider,
        "depth_data_provider": provider,
        "adj_factor_provider": "same_as_daily",
    })
    return provider


def get_daily_data_provider() -> str:
    return _clean_data_provider(load().get("daily_data_provider", get_data_provider()))


def get_adj_factor_provider() -> str:
    provider = str(load().get("adj_factor_provider", "same_as_daily") or "same_as_daily").lower()
    if provider == "same_as_daily":
        return provider
    return _clean_data_provider(provider)


def get_minute_data_provider() -> str:
    return _clean_data_provider(load().get("minute_data_provider", get_data_provider()))


def get_realtime_data_provider() -> str:
    return _clean_data_provider(load().get("realtime_data_provider", get_data_provider()))


def get_financial_data_provider() -> str:
    return _clean_data_provider(load().get("financial_data_provider", get_data_provider()))


def get_depth_data_provider() -> str:
    return _clean_data_provider(load().get("depth_data_provider", get_data_provider()))


# ===== 盘后管道拉取内容开关 (A股 / ETF / 指数 独立控制) =====

def get_pipeline_pull_a_share() -> bool:
    """A 股日K固定拉取。"""
    return True


def get_pipeline_pull_etf() -> bool:
    """是否拉取 ETF 日K。默认 False(标的多,首次较慢)。"""
    return load().get("pipeline_pull_etf", False)


def get_pipeline_pull_hk() -> bool:
    """是否拉取港股日K。默认 False —— 港股是可选能力,且本地无除权数据源
    (仅不复权),用户需明确知情后再开启。"""
    return load().get("pipeline_pull_hk", False)


def get_pipeline_pull_index() -> bool:
    """是否拉取指数日K。默认 True。"""
    return load().get("pipeline_pull_index", True)


_PIPELINE_PULL_KEYS = ("pipeline_pull_etf", "pipeline_pull_index", "pipeline_pull_hk")


def get_pipeline_pull_types() -> dict:
    """返回四个拉取开关的当前值。"""
    return {
        "pipeline_pull_a_share": get_pipeline_pull_a_share(),
        "pipeline_pull_etf": get_pipeline_pull_etf(),
        "pipeline_pull_index": get_pipeline_pull_index(),
        "pipeline_pull_hk": get_pipeline_pull_hk(),
    }


def set_pipeline_pull_types(cfg: dict) -> dict:
    """批量保存拉取开关。只接受白名单内的布尔字段。"""
    updates = {
        k: bool(v) for k, v in cfg.items()
        if k in _PIPELINE_PULL_KEYS and v is not None
    }
    save(updates)
    return get_pipeline_pull_types()




def get_pipeline_schedule() -> dict:
    """返回盘后管道调度时间 {"hour": 15, "minute": 30}。"""
    d = load().get("pipeline_schedule", {"hour": 15, "minute": 30})
    return {"hour": d.get("hour", 15), "minute": d.get("minute", 30)}


def set_pipeline_schedule(hour: int, minute: int) -> dict:
    h = max(0, min(23, hour))
    m = max(0, min(59, minute))
    # 盘后不早于 15:00
    if h * 60 + m < 15 * 60:
        h, m = 15, 0
    save({"pipeline_schedule": {"hour": h, "minute": m}})
    return {"hour": h, "minute": m}


def get_instruments_schedule() -> dict:
    """返回盘前标的维表调度时间 {"hour": 9, "minute": 10}。"""
    d = load().get("instruments_schedule", {"hour": 9, "minute": 10})
    return {"hour": d.get("hour", 9), "minute": d.get("minute", 10)}


def set_instruments_schedule(hour: int, minute: int) -> dict:
    h = max(0, min(23, hour))
    m = max(0, min(59, minute))
    # 盘前不晚于 09:15
    if h * 60 + m > 9 * 60 + 15:
        h, m = 9, 15
    save({"instruments_schedule": {"hour": h, "minute": m}})
    return {"hour": h, "minute": m}


def get_enriched_batch_size() -> int:
    """返回 enriched 全量计算每批 symbol 数量。"""
    return max(1, min(10000, load().get("enriched_batch_size", 1000)))


def set_enriched_batch_size(size: int) -> int:
    """保存 enriched 全量计算批次大小。"""
    size = max(10, min(6000, size))
    save({"enriched_batch_size": size})
    return size


def get_index_daily_batch_size() -> int:
    """返回指数日 K 同步每批 symbol 数量。"""
    return max(1, min(10000, load().get("index_daily_batch_size", 100)))


def set_index_daily_batch_size(size: int) -> int:
    """保存指数日 K 同步批次大小。"""
    size = max(1, min(10000, size))
    save({"index_daily_batch_size": size})
    return size


# ── 五档盘口 sealed(真假涨停) 配置 ──────────────────────

def get_limit_ladder_monitor_enabled() -> bool:
    """连板梯队 5 档监控开关。关闭时 depth 不轮询(连板梯队降级显示)。"""
    return load().get("limit_ladder_monitor_enabled", False)


def get_depth_polling_interval() -> float:
    """depth 盘中轮询间隔(秒)。默认 20。"""
    return float(load().get("depth_polling_interval", 20.0))


def set_depth_polling_interval(interval: float) -> float:
    """保存 depth 轮询间隔。能力范围 clamp 由 depth_service 执行。"""
    interval = max(1.0, min(600.0, float(interval)))
    save({"depth_polling_interval": interval})
    return interval


def get_depth_finalize_time() -> dict:
    """盘后 sealed 定版时间 {"hour": 15, "minute": 2}。范围 15:01~18:00。"""
    d = load().get("depth_finalize_time", {"hour": 15, "minute": 2})
    return {"hour": d.get("hour", 15), "minute": d.get("minute", 2)}


def set_depth_finalize_time(hour: int, minute: int) -> dict:
    """保存盘后 sealed 定版时间,强制范围 15:01~18:00。"""
    h = max(0, min(23, hour))
    m = max(0, min(59, minute))
    # 下限 15:01, 上限 18:00
    if h * 60 + m < 15 * 60 + 1:
        h, m = 15, 1
    if h * 60 + m > 18 * 60:
        h, m = 18, 0
    save({"depth_finalize_time": {"hour": h, "minute": m}})
    return {"hour": h, "minute": m}


# 复盘推送可选渠道白名单。
# 多选: 不推送 = 空数组, 而非 'none'
REVIEW_PUSH_CHANNELS = {"feishu", "dingtalk", "wecom", "meow", "pushplus"}


def get_review_schedule() -> dict:
    """定时复盘调度 {"enabled": False, "hour": 15, "minute": 10}。默认关闭。

    A股 15:00 收盘, 默认时间设为 15:10(收盘后即时复盘), 强制下限 15:00。
    """
    d = load().get("review_schedule", {"enabled": False, "hour": 15, "minute": 10})
    return {
        "enabled": bool(d.get("enabled", False)),
        "hour": d.get("hour", 15),
        "minute": d.get("minute", 10),
    }


def set_review_schedule(enabled: bool, hour: int, minute: int) -> dict:
    """保存定时复盘调度。强制时间下限 15:00(A股收盘)。

    enabled=False 时时间仍保存(下次开启可沿用), 但调度器不会注册 job。
    """
    h = max(0, min(23, hour))
    m = max(0, min(59, minute))
    # 下限 15:00: A股 15:00 收盘, 收盘后才有当日完整数据复盘
    if h * 60 + m < 15 * 60:
        h, m = 15, 0
    save({"review_schedule": {"enabled": bool(enabled), "hour": h, "minute": m}})
    return {"enabled": bool(enabled), "hour": h, "minute": m}


def get_review_push_channels() -> list[str]:
    """复盘推送渠道(多选) — 选定的外部工具列表, 复盘归档后逐个推送。

    与 review_schedule / 实时行情完全独立, 常驻可单独设置。
    空列表 = 不推送; ['feishu'] = 推送到飞书(复用监控中心全局 feishu_webhook_url/secret)。

    向后兼容:
      - 老多版本单选 review_push_channel=='feishu' → ['feishu']
      - 更老布尔 review_push_enabled==True → ['feishu']
    """
    d = load()
    raw = d.get("review_push_channels")
    if isinstance(raw, list):
        return [c for c in raw if c in REVIEW_PUSH_CHANNELS]
    # 兼容老单选字符串
    if d.get("review_push_channel") == "feishu":
        return ["feishu"]
    # 兼容更老布尔开关
    if d.get("review_push_enabled") is True:
        return ["feishu"]
    return []


def set_review_push_channels(channels: list[str]) -> list[str]:
    """保存复盘推送渠道(多选)。过滤白名单外的值、去重、保序。空列表 = 不推送。"""
    seen: set[str] = set()
    cleaned: list[str] = []
    for c in channels or []:
        if c in REVIEW_PUSH_CHANNELS and c not in seen:
            seen.add(c)
            cleaned.append(c)
    save({"review_push_channels": cleaned})
    return cleaned


# ===== 交易自动复盘 (P6.4 L0/L1/L2 状态驱动 AI 归因) =====

def get_trading_auto_review() -> bool:
    """交易自动复盘开关。默认 False —— 盘后状态驱动归因依赖 AI,
    且会消耗 token, 由用户明确开启。"""
    return bool(load().get("tradingAutoReview", False))


def set_trading_auto_review(enabled: bool) -> bool:
    """保存交易自动复盘开关。"""
    save({"tradingAutoReview": bool(enabled)})
    return bool(enabled)



# ===== 实时监控 =====

# 页面 SSE 刷新配置: { "watchlist": true, "monitor": true, ... }
# 可刷新的页面列表及其默认值
SSE_REFRESH_PAGES_DEFAULT = {
    "watchlist": True,
    "limit-ladder": False,
}

SIDEBAR_INDEX_SYMBOLS_DEFAULT = ["000001.INDEX", "399001.INDEX", "399006.INDEX", "000680.INDEX"]


# ===== 盘中实时行情范围 (独立于盘后管道范围) =====


def get_realtime_pull_stock() -> bool:
    return load().get("realtime_pull_stock", True)


def get_realtime_pull_etf() -> bool:
    # 老用户兼容: ETF 实时默认关闭，避免升级后请求量/写盘量突然增加。
    return load().get("realtime_pull_etf", False)


def get_realtime_pull_index() -> bool:
    return load().get("realtime_pull_index", True)


def get_realtime_index_mode() -> str:
    mode = str(load().get("realtime_index_mode", "core") or "core").lower()
    return mode if mode in {"core", "all"} else "core"


def get_realtime_index_symbols() -> list[str]:
    from app.data_providers.fquant.symbols import canonical_index_symbol
    stored = load().get("realtime_index_symbols", SIDEBAR_INDEX_SYMBOLS_DEFAULT)
    if isinstance(stored, str):
        import re
        stored = [s.strip() for s in re.split(r"[,\s]+", stored) if s.strip()]
    # 兼容旧存量值: 内存规范化为 .INDEX (不回写磁盘)
    return [canonical_index_symbol(str(s)) for s in stored if str(s).strip()]


def set_realtime_quote_scope(cfg: dict) -> dict:
    updates = {}
    for key in ("realtime_pull_stock", "realtime_pull_etf", "realtime_pull_index"):
        if key in cfg and cfg[key] is not None:
            updates[key] = bool(cfg[key])
    if "realtime_index_mode" in cfg and cfg["realtime_index_mode"] in {"core", "all"}:
        updates["realtime_index_mode"] = cfg["realtime_index_mode"]
    if "realtime_index_symbols" in cfg and cfg["realtime_index_symbols"] is not None:
        updates["realtime_index_symbols"] = cfg["realtime_index_symbols"]
    if updates:
        save(updates)
    return get_realtime_quote_scope()


def get_realtime_quote_scope() -> dict:
    return {
        "realtime_pull_stock": get_realtime_pull_stock(),
        "realtime_pull_etf": get_realtime_pull_etf(),
        "realtime_pull_index": get_realtime_pull_index(),
        "realtime_index_mode": get_realtime_index_mode(),
        "realtime_index_symbols": get_realtime_index_symbols(),
    }


def get_sse_refresh_pages() -> dict[str, bool]:
    """返回每个页面的 SSE 刷新开关。"""
    stored = load().get("sse_refresh_pages", {})
    # 合并默认值 (新增页面自动出现)
    result = dict(SSE_REFRESH_PAGES_DEFAULT)
    result.update(stored)
    return result


def set_sse_refresh_pages(pages: dict[str, bool]) -> dict[str, bool]:
    """保存页面 SSE 刷新配置。"""
    save({"sse_refresh_pages": pages})
    return get_sse_refresh_pages()


def get_sidebar_index_symbols() -> list[str]:
    """返回左侧菜单显示的 canonical 指数代码。"""
    import re

    from app.data_providers.fquant.symbols import canonical_index_symbol

    stored = load().get("sidebar_index_symbols", SIDEBAR_INDEX_SYMBOLS_DEFAULT)
    if isinstance(stored, str):
        stored = [s for s in re.split(r"[,\s]+", stored) if s]
    if not isinstance(stored, list):
        stored = SIDEBAR_INDEX_SYMBOLS_DEFAULT
    allowed = set(SIDEBAR_INDEX_SYMBOLS_DEFAULT)
    normalized = (
        canonical_index_symbol(str(s))
        for s in stored
        if str(s).strip()
    )
    return list(dict.fromkeys(s for s in normalized if s in allowed))


def get_strategy_monitor_enabled() -> bool:
    """策略告警评估总开关。"""
    return load().get("strategy_monitor_enabled", False)


def get_system_notify_enabled() -> bool:
    """系统通知开关 — 开启后监控告警同时推送到操作系统通知中心。"""
    return load().get("system_notify_enabled", False)


def set_system_notify_enabled(enabled: bool) -> bool:
    """保存系统通知开关。"""
    save({"system_notify_enabled": bool(enabled)})
    return bool(enabled)


def get_feishu_webhook_url() -> str:
    """飞书自定义机器人 Webhook 地址 — 全局共用一处, 所有启用推送的规则都推到这一个群。"""
    return load().get("feishu_webhook_url", "")


def get_feishu_webhook_secret() -> str:
    """飞书自定义机器人签名密钥 — 机器人启用「签名校验」时必填, 留空表示不验签。"""
    return load().get("feishu_webhook_secret", "")


def set_feishu_webhook_url(url: str) -> str:
    """保存飞书 Webhook 地址。传入空串表示清空配置。"""
    save({"feishu_webhook_url": str(url or "").strip()})
    return get_feishu_webhook_url()


def set_feishu_webhook_secret(secret: str) -> str:
    """保存飞书签名密钥。传入空串表示不验签。"""
    save({"feishu_webhook_secret": str(secret or "").strip()})
    return get_feishu_webhook_secret()

# ── PushPlus (M18): token 存于 secrets.json (0600), 不入 preferences.json ──

_PUSHPLUS_SECRET_KEY = "pushplus_token"


def get_pushplus_token() -> str:
    """PushPlus token — 从 secrets.json 读取 (0600), 绝不回退到 preferences.json。"""
    from app import secrets_store
    return str(secrets_store.load().get(_PUSHPLUS_SECRET_KEY, "") or "")


def get_pushplus_status() -> dict:
    """PushPlus 对外可见状态: configured + masked token (不含真实 token)。"""
    from app import secrets_store
    token = get_pushplus_token()
    return {
        "configured": bool(token),
        "token_masked": secrets_store.mask(token) if token else "",
    }


def set_pushplus_token(token: str) -> None:
    """保存 PushPlus token 到 secrets.json (0600)。"""
    from app import secrets_store
    secrets_store.save({_PUSHPLUS_SECRET_KEY: str(token or "").strip()})


def clear_pushplus_token() -> None:
    """从 secrets.json 删除 PushPlus token。"""
    from app import secrets_store
    secrets_store.clear(_PUSHPLUS_SECRET_KEY)


def get_webhook_channels() -> dict:
    """返回所有 webhook 通道配置；兼容旧 feishu 字段。

    PushPlus 例外: token 存于 secrets.json, 对外只暴露 configured/token_masked。
    """
    channels = load().get("webhook_channels", {})
    if not isinstance(channels, dict):
        channels = {}
    out = {k: v for k, v in channels.items() if k in REVIEW_PUSH_CHANNELS and isinstance(v, dict)}
    # PushPlus token 永远存于 secrets.json, 不从 preferences.json 读取 (防泄漏)
    out.pop("pushplus", None)
    out["feishu"] = {
        "url": get_feishu_webhook_url(),
        "secret": get_feishu_webhook_secret(),
    }
    out["pushplus"] = get_pushplus_status()
    return out


def get_configured_webhook_channels() -> dict:
    """返回已配置通道 (含真实凭据), 供 adapter 发送。

    PushPlus: 从 secrets.json 注入真实 token; 其他通道复用 get_webhook_channels 的 url/nickname。
    """
    out = {}
    for k, v in get_webhook_channels().items():
        if k == "pushplus":
            token = get_pushplus_token()
            if token:
                out[k] = {"token": token}
        elif (v.get("url") or v.get("nickname") or "").strip():
            out[k] = v
    return out


def set_webhook_channel(channel: str, config: dict) -> dict:
    """保存单个 webhook 通道。feishu 仍写旧字段，其他写 webhook_channels。

    PushPlus 特殊处理: token 存于 secrets.json (0600), 不入 preferences.json。
    - token 非空 → 保存到 secrets.json
    - clear_token=True → 从 secrets.json 删除
    - token 空 + clear_token=False → 保留旧 token (无操作)
    """
    channel = str(channel or "").strip().lower()
    if channel not in REVIEW_PUSH_CHANNELS:
        raise ValueError(f"unsupported webhook channel: {channel}")
    if channel == "pushplus":
        token = str(config.get("token") or "").strip()
        clear_token = bool(config.get("clear_token"))
        if clear_token:
            clear_pushplus_token()
        elif token:
            set_pushplus_token(token)
        return get_pushplus_status()
    cleaned = {
        "url": str(config.get("url") or "").strip(),
        "secret": str(config.get("secret") or "").strip(),
        "nickname": str(config.get("nickname") or "").strip(),
    }
    if channel == "feishu":
        set_feishu_webhook_url(cleaned["url"])
        set_feishu_webhook_secret(cleaned["secret"])
        return get_webhook_channels()["feishu"]
    current = load().get("webhook_channels", {})
    if not isinstance(current, dict):
        current = {}
    current[channel] = cleaned
    save({"webhook_channels": current})
    return get_webhook_channels()[channel]


def get_webhook_enabled_default() -> bool:
    """新建监控规则时是否默认勾选「Webhook 推送」。

    数据模型当前只有一个 webhook_enabled 布尔；启用后推送到所有已配置通道。
    此默认值供规则编辑器新建规则时预填, 单条规则仍可独立修改。
    """
    return load().get("webhook_enabled_default", False)


def set_webhook_enabled_default(enabled: bool) -> bool:
    """保存飞书推送默认勾选态。"""
    save({"webhook_enabled_default": bool(enabled)})
    return get_webhook_enabled_default()


def get_screener_auto_run() -> bool:
    """选股页进入时是否自动运行所有策略 (获取命中数)。默认开。"""
    return load().get("screener_auto_run", True)


def get_strategy_monitor_ids() -> list[str]:
    """返回监控池中的策略 ID。"""
    return load().get("strategy_monitor_ids", [])


def set_realtime_monitor_config(cfg: dict) -> dict:
    """批量更新实时监控配置。"""
    updates = {}
    if "sse_refresh_pages" in cfg:
        updates["sse_refresh_pages"] = cfg["sse_refresh_pages"]
    if "strategy_monitor_enabled" in cfg:
        updates["strategy_monitor_enabled"] = cfg["strategy_monitor_enabled"]
    if "strategy_monitor_ids" in cfg:
        updates["strategy_monitor_ids"] = cfg["strategy_monitor_ids"]
    if "sidebar_index_symbols" in cfg:
        allowed = set(SIDEBAR_INDEX_SYMBOLS_DEFAULT)
        updates["sidebar_index_symbols"] = [s for s in cfg["sidebar_index_symbols"] if s in allowed]
    if "screener_auto_run" in cfg:
        updates["screener_auto_run"] = bool(cfg["screener_auto_run"])
    if updates:
        save(updates)
    return get_realtime_monitor_config()


def get_realtime_monitor_config() -> dict:
    """返回完整的实时监控配置。"""
    return {
        "sse_refresh_pages": get_sse_refresh_pages(),
        "strategy_monitor_enabled": get_strategy_monitor_enabled(),
        "strategy_monitor_ids": get_strategy_monitor_ids(),
        "sidebar_index_symbols": get_sidebar_index_symbols(),
        "screener_auto_run": get_screener_auto_run(),
    }


def get_nav_order() -> list[str]:
    """返回左侧菜单的自定义排序（内置页面 path + 扩展分析菜单 id）。"""
    return load().get("nav_order", [])


def set_nav_order(order: list[str]) -> list[str]:
    """保存左侧菜单排序。"""
    save({"nav_order": order})
    return get_nav_order()


def get_nav_hidden() -> list[str]:
    """返回左侧菜单中隐藏的项 id 列表。"""
    return load().get("nav_hidden", [])


def set_nav_hidden(hidden: list[str]) -> list[str]:
    """保存左侧菜单隐藏项。"""
    save({"nav_hidden": hidden})
    return get_nav_hidden()


def get_watchlist_columns() -> list[dict] | None:
    """返回自选列表列配置。"""
    return load().get("watchlist_columns")


def set_watchlist_columns(columns: list[dict]) -> list[dict]:
    """保存自选列表列配置。"""
    save({"watchlist_columns": columns})
    return columns


def get_screener_result_columns() -> list[dict] | None:
    """返回策略结果列表列配置。"""
    return load().get("screener_result_columns")


def set_screener_result_columns(columns: list[dict]) -> list[dict]:
    """保存策略结果列表列配置。"""
    save({"screener_result_columns": columns})
    return columns


# ===== 首次使用引导 =====

def get_onboarding_completed() -> bool:
    """是否已完成首次使用向导。默认 False（新用户）。"""
    return bool(load().get("onboarding_completed", False))


def set_onboarding_completed(done: bool = True) -> bool:
    """标记首次使用向导完成状态。"""
    save({"onboarding_completed": bool(done)})
    return bool(done)


# ===== 财务数据同步时间(持久化,重启不丢失) =====
# 结构: { "metrics": "2026-06-25T10:00:00+08:00", "income": ..., ... }

def get_financial_sync_times() -> dict[str, str]:
    """返回各财务表的最后同步时间(ISO 字符串)。未同步过的表不在返回值中。"""
    return load().get("financial_sync_times", {}) or {}


def set_financial_sync_time(table: str, iso_ts: str) -> None:
    """更新单张财务表的最后同步时间(合并写入,不清除其他表)。"""
    times = get_financial_sync_times()
    times[table] = iso_ts
    save({"financial_sync_times": times})



# ===== AI profile 路由策略 (P3 显式受控 fallback，默认关闭) =====

def get_ai_route_policy() -> dict:
    """返回当前 route policy。默认关闭。存储于 preferences（非密钥数据）。"""
    d = load().get("ai_route_policy") or {}
    return {
        "allow_profile_fallback": bool(d.get("allow_profile_fallback", False)),
        "fallback_profile_ids": [str(x) for x in (d.get("fallback_profile_ids") or []) if str(x).strip()],
    }


def set_ai_route_policy(allow_profile_fallback: bool, fallback_profile_ids: list[str]) -> dict:
    """保存策略（内部去重保序）。返回规范化值。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for x in (fallback_profile_ids or []):
        s = str(x).strip()
        if s and s not in seen:
            seen.add(s)
            cleaned.append(s)
    save({"ai_route_policy": {"allow_profile_fallback": bool(allow_profile_fallback), "fallback_profile_ids": cleaned}})
    return get_ai_route_policy()


# ===== 结构化计划检查 (P4 默认关闭) =====

def get_structured_plan_check_enabled() -> bool:
    """结构化计划检查开关。默认 False —— AI 双阶段检查会消耗 token,
    且涉及交易决策辅助, 由用户明确开启。"""
    return bool(load().get("structured_plan_check_enabled", False))


def set_structured_plan_check_enabled(enabled: bool) -> bool:
    """保存结构化计划检查开关。"""
    save({"structured_plan_check_enabled": bool(enabled)})
    return bool(enabled)


# ===== 受控外部 fallback (默认关闭) =====
# 完整契约见 backend/docs/CONTROLLED_EXTERNAL_FALLBACK_DESIGN.md 与 AGENTS.md §4。
# realtime/depth scope 均已接线；外部数据只用于只读展示，绝不写入
# canonical/enriched/选股/监控/回测。

# 合法 scope 白名单 (契约: 仅 realtime/depth 子集)。
_EXTERNAL_FALLBACK_SCOPES_ALLOWED = ("realtime", "depth")


def get_external_fallback_enabled() -> bool:
    """受控外部 fallback 总开关。默认 False —— 用户显式开启后对应 scope 才激活。"""
    return bool(load().get("external_fallback_enabled", False))


def get_external_fallback_scopes() -> list[str]:
    """已启用的 fallback scope 子集 (realtime/depth 白名单内), 去重保序。

    默认空列表 (即便 enabled=True, 也需 scope 显式包含才触发)。
    非白名单值被静默过滤 (读取侧防御; 写入侧 400 拒绝)。
    """
    stored = load().get("external_fallback_scopes", []) or []
    seen: set[str] = set()
    out: list[str] = []
    for s in stored:
        key = str(s).strip().lower()
        if key in _EXTERNAL_FALLBACK_SCOPES_ALLOWED and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def set_external_fallback(enabled: bool, scopes: list[str]) -> tuple[bool, list[str]]:
    """保存受控外部 fallback 偏好。

    enabled: 总开关。scopes: 启用 scope 子集 (白名单内, 去重保序)。
    返回清洗后的 (enabled, scopes)。非法 scope 抛 ValueError (API 层转 400)。
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for s in scopes or []:
        key = str(s).strip().lower()
        if not key:
            continue
        if key not in _EXTERNAL_FALLBACK_SCOPES_ALLOWED:
            raise ValueError(f"invalid external_fallback scope: {key}")
        if key not in seen:
            seen.add(key)
            cleaned.append(key)
    enabled_b = bool(enabled)
    save({"external_fallback_enabled": enabled_b, "external_fallback_scopes": cleaned})
    return enabled_b, cleaned

# ===== 信号记分卡 (Signal Scorecard, opt-in 默认空) =====
# 记分卡是回顾性分析工具: 把布尔技术信号实例化为不可变事件, 再用本地 enriched
# 前向 N 个交易日收盘价计算 hit/miss/neutral。tracked_signals 白名单 + 默认空
# 防止全市场信号洪泛。不接 provider、不生成荐股/买卖建议。

_SIGNAL_DIRECTION_ALLOWED = ("up", "not_up")


def get_tracked_signals() -> list[dict]:
    """返回信号记分卡跟踪的信号列表。默认空 (opt-in)。

    每项: {signal_key, signal_name, signal_kind, direction, enabled}
      - signal_key: enriched 布尔列名 (signal_* 内置 / csg_* 自定义)
      - signal_kind: entry | exit | builtin (决定默认方向推断)
      - direction: "up"/"not_up" 覆盖推断, 或 None (按 kind 推断)
      - enabled: 是否参与实例化/评估
    """
    stored = load().get("tracked_signals", []) or []
    out: list[dict] = []
    for t in stored:
        if not isinstance(t, dict):
            continue
        sk = str(t.get("signal_key", "")).strip()
        if not sk:
            continue
        d = t.get("direction")
        out.append({
            "signal_key": sk,
            "signal_name": str(t.get("signal_name", sk)),
            "signal_kind": str(t.get("signal_kind", "builtin")),
            "direction": d if d in _SIGNAL_DIRECTION_ALLOWED else None,
            "enabled": bool(t.get("enabled", True)),
        })
    return out


def set_tracked_signals(items: list[dict]) -> list[dict]:
    """保存跟踪信号列表 (去重保序, 过滤空 signal_key)。返回清洗后的列表。"""
    cleaned: list[dict] = []
    seen: set[str] = set()
    for t in items or []:
        if not isinstance(t, dict):
            continue
        sk = str(t.get("signal_key", "")).strip()
        if not sk or sk in seen:
            continue
        seen.add(sk)
        d = t.get("direction")
        cleaned.append({
            "signal_key": sk,
            "signal_name": str(t.get("signal_name", sk)),
            "signal_kind": str(t.get("signal_kind", "builtin")),
            "direction": d if d in _SIGNAL_DIRECTION_ALLOWED else None,
            "enabled": bool(t.get("enabled", True)),
        })
    save({"tracked_signals": cleaned})
    return get_tracked_signals()