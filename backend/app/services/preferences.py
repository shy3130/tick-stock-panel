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
    return load().get("realtime_quote_interval", 6.0)


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


def set_realtime_watchlist_symbols(symbols: list[str]) -> list[str]:  # noqa: ARG001
    """兼容旧接口: Free 实时标的现在由自选页前 5 个决定。"""
    return get_realtime_watchlist_symbols()


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


def get_minute_intraday_refresh() -> bool:
    """自选列表分时图是否跟随实时行情刷新。

    默认值随权限: 有实时行情权限 (Pro+) 的用户默认开启, 否则关闭。
    用户主动设置过的 (key 存在) 以用户选择为准, 即使是 False 也尊重。
    """
    data = load()
    if "minute_intraday_refresh" in data:
        return bool(data["minute_intraday_refresh"])
    # 未设置过: 有权限默认开, 无权限默认关。
    try:
        from app.services.quote_service import QuoteService
        return QuoteService.is_realtime_allowed()
    except Exception:
        return False


# 分时图实时刷新间隔允许范围 (秒)。下限 3s, 上限 60s。
_INTRADAY_REFRESH_INTERVAL_MIN = 3
_INTRADAY_REFRESH_INTERVAL_MAX = 60


def get_minute_intraday_refresh_interval() -> int:
    """分时图实时刷新轮询间隔 (秒)。默认 6s, 范围 [3, 60]。"""
    return max(_INTRADAY_REFRESH_INTERVAL_MIN,
               min(_INTRADAY_REFRESH_INTERVAL_MAX,
                   int(load().get("minute_intraday_refresh_interval", 6))))


# 监控中心个股通知 ext 字段默认配置 (与 ext_presets 内置预设对齐)
_MONITOR_EXT_FIELDS_DEFAULT = {
    "concept": "ext_gn_ths.所属概念",
    "industry": "ext_hy_ths.所属同话顺行业",
}


def _normalize_ext_field(raw) -> dict | None:
    """规范化单个 ext 字段配置, 兼容旧字符串格式 ("id.field") 和新对象格式。

    新格式: {"field": "id.field", "maxTags": N, "hiddenIndices": [...]}
    maxTags=0 或缺省=不限制; hiddenIndices 指定要隐藏的位置 (0-based)。
    """
    if raw is None:
        return None
    # 旧格式: 纯字符串 "configId.fieldName"
    if isinstance(raw, str):
        return {"field": raw}
    if isinstance(raw, dict):
        field = raw.get("field")
        if not field:
            return None
        return {
            "field": field,
            "maxTags": int(raw["maxTags"]) if raw.get("maxTags") else 0,
            "hiddenIndices": [int(i) for i in raw["hiddenIndices"]] if raw.get("hiddenIndices") else [],
        }
    return None


def get_monitor_ext_fields() -> dict:
    """监控中心个股通知要展示的 ext 字段 (concept/industry)。

    返回 {"concept": {"field", "maxTags", "hiddenIndices"} | None, ...}。
    后端只需读 .field 构建 ext_columns; maxTags/hiddenIndices 供前端渲染裁剪。
    兼容旧字符串格式 ("id.field") 自动升级。
    """
    data = load()
    raw = data.get("monitor_ext_fields")
    if raw is None:
        return {
            "concept": {"field": _MONITOR_EXT_FIELDS_DEFAULT["concept"]},
            "industry": {"field": _MONITOR_EXT_FIELDS_DEFAULT["industry"]},
        }
    return {
        "concept": _normalize_ext_field(raw.get("concept")),
        "industry": _normalize_ext_field(raw.get("industry")),
    }


def get_minute_sync_days() -> int:
    return max(1, min(30, load().get("minute_sync_days", 5)))


def get_minute_sync_segment_days() -> int:
    """分钟 K 拉取的单段大小(交易日)。默认 20,范围 [5, 30]。

    每段拉完后立即落盘(流式),避免全量攒内存导致 OOM。
    段越小内存峰值越低但总耗时越长(限速 sleep 随段数线性增加);
    物理上限 ~41 交易日(TickFlow 单次 10000 根 / 一天 241 根 ≈ 41 天),max=30 留出余量。
    """
    return max(5, min(30, load().get("minute_sync_segment_days", 20)))


# ===== 数据源选择 (默认 TickFlow；第一阶段仅日K切换入口) =====

_ALLOWED_DATA_PROVIDERS = {"tickflow"}


def _allowed_data_providers() -> set[str]:
    try:
        from app.data_providers import custom as custom_sources
        return _ALLOWED_DATA_PROVIDERS | custom_sources.names()
    except Exception:  # noqa: BLE001
        return set(_ALLOWED_DATA_PROVIDERS)


def get_daily_data_provider() -> str:
    provider = str(load().get("daily_data_provider", "tickflow") or "tickflow").lower()
    return provider if provider in _allowed_data_providers() else "tickflow"


def get_adj_factor_provider() -> str:
    provider = str(load().get("adj_factor_provider", "same_as_daily") or "same_as_daily").lower()
    if provider == "same_as_daily":
        return provider
    return provider if provider in _allowed_data_providers() else "same_as_daily"


def get_minute_data_provider() -> str:
    provider = str(load().get("minute_data_provider", "tickflow") or "tickflow").lower()
    return provider if provider in _allowed_data_providers() else "tickflow"


def get_realtime_data_provider() -> str:
    provider = str(load().get("realtime_data_provider", "tickflow") or "tickflow").lower()
    return provider if provider in _allowed_data_providers() else "tickflow"


def get_financial_provider() -> str:
    provider = str(load().get("financial_data_provider", "tickflow") or "tickflow").lower()
    return provider if provider in _allowed_data_providers() else "tickflow"


# ===== 盘后管道拉取内容开关 (A股 / ETF / 指数 独立控制) =====

def get_pipeline_pull_a_share() -> bool:
    """A 股日K固定拉取。"""
    return True


def get_pipeline_pull_etf() -> bool:
    """是否拉取 ETF 日K。默认 False(标的多,首次较慢)。"""
    return load().get("pipeline_pull_etf", False)


def get_pipeline_pull_index() -> bool:
    """是否拉取指数日K。默认 True。"""
    return load().get("pipeline_pull_index", True)


_PIPELINE_PULL_KEYS = ("pipeline_pull_etf", "pipeline_pull_index")


def get_pipeline_pull_types() -> dict:
    """返回三个拉取开关的当前值。"""
    return {
        "pipeline_pull_a_share": get_pipeline_pull_a_share(),
        "pipeline_pull_etf": get_pipeline_pull_etf(),
        "pipeline_pull_index": get_pipeline_pull_index(),
    }


def set_pipeline_pull_types(cfg: dict) -> dict:
    """批量保存拉取开关。只接受白名单内的布尔字段。"""
    updates = {
        k: bool(v) for k, v in cfg.items()
        if k in _PIPELINE_PULL_KEYS and v is not None
    }
    save(updates)
    return get_pipeline_pull_types()


def get_pipeline_index_symbols() -> str:
    """指数自定义拉取代码(逗号/换行/空格分隔)。空串表示全量。"""
    return str(load().get("pipeline_index_symbols", "") or "").strip()


def set_pipeline_index_symbols(symbols: str) -> str:
    """保存指数自定义代码,返回规范化后的字符串。"""
    save({"pipeline_index_symbols": symbols})
    return get_pipeline_index_symbols()


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
    """depth 盘中轮询间隔(秒)。默认 10(Pro/Expert 都适用)。"""
    return float(load().get("depth_polling_interval", 10.0))


def set_depth_polling_interval(interval: float) -> float:
    """保存 depth 轮询间隔。套餐范围 clamp 由 depth_service 按档位做。"""
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


# 复盘推送可选渠道白名单 (企业微信已实现, 与飞书并列)
# 多选: 不推送 = 空数组, 而非 'none'
REVIEW_PUSH_CHANNELS = {"feishu", "wecom"}


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



# ===== 实时监控 =====

# 页面 SSE 刷新配置: { "watchlist": true, "monitor": true, ... }
# 可刷新的页面列表及其默认值
SSE_REFRESH_PAGES_DEFAULT = {
    "watchlist": True,
    "limit-ladder": False,
}

SIDEBAR_INDEX_SYMBOLS_DEFAULT = ["000001.SH", "399001.SZ", "399006.SZ", "000680.SH"]


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
    stored = load().get("realtime_index_symbols", SIDEBAR_INDEX_SYMBOLS_DEFAULT)
    if isinstance(stored, str):
        import re
        stored = [s.strip() for s in re.split(r"[,\s]+", stored) if s.strip()]
    return [str(s) for s in stored if str(s).strip()]


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
    """返回左侧菜单显示的指数代码。"""
    stored = load().get("sidebar_index_symbols", SIDEBAR_INDEX_SYMBOLS_DEFAULT)
    allowed = set(SIDEBAR_INDEX_SYMBOLS_DEFAULT)
    return [s for s in stored if s in allowed]


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


def get_wecom_webhook_url() -> str:
    """企业微信群推送 Webhook 地址 — 与飞书并列的第二推送通道。

    存储完整 URL (https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx);
    用户也可只填 key, 由 webhook_adapter.normalize_wecom_url 自动补全。
    """
    return load().get("wecom_webhook_url", "")


def set_wecom_webhook_url(url: str) -> str:
    """保存企业微信 Webhook 地址。传入空串表示清空配置。

    存储时统一补全为完整 URL, 避免后续每次推送都要再判一次。
    """
    from app.services.webhook_adapter import normalize_wecom_url
    save({"wecom_webhook_url": normalize_wecom_url(url)})
    return get_wecom_webhook_url()


# ===== 企业微信智能机器人 (API 模式 / 长连接) =====


def get_wecom_bot_id() -> str:
    """企业微信智能机器人 BotID — 机器人的唯一标识。"""
    return load().get("wecom_bot_id", "")


def set_wecom_bot_id(bot_id: str) -> str:
    """保存智能机器人 BotID。传入空串表示清空。"""
    save({"wecom_bot_id": (bot_id or "").strip()})
    return get_wecom_bot_id()


def get_wecom_bot_secret() -> str:
    """企业微信智能机器人 Secret — 长连接专用密钥。"""
    return load().get("wecom_bot_secret", "")


def set_wecom_bot_secret(secret: str) -> str:
    """保存智能机器人 Secret。传入空串表示清空。"""
    save({"wecom_bot_secret": (secret or "").strip()})
    return get_wecom_bot_secret()


def get_wecom_bot_enabled() -> bool:
    """智能机器人长连接是否启用。默认 False(需用户配置凭证后手动开启)。"""
    return load().get("wecom_bot_enabled", False)


def set_wecom_bot_enabled(enabled: bool) -> bool:
    """保存智能机器人启用状态。"""
    save({"wecom_bot_enabled": bool(enabled)})
    return get_wecom_bot_enabled()



def get_webhook_enabled_default() -> bool:
    """新建监控规则时是否默认勾选推送 (老布尔, 已由 webhook_default_channels 取代)。

    保留向后兼容: 读取 webhook_default_channels 非空时返回 True。
    """
    return bool(get_webhook_default_channels())


def set_webhook_enabled_default(enabled: bool) -> bool:
    """保存推送默认勾选态 (老布尔兼容入口)。

    新数据模型为渠道数组; 此处把老布尔转译: True→['feishu','wecom'], False→[]。
    """
    set_webhook_default_channels(["feishu", "wecom"] if enabled else [])
    return get_webhook_enabled_default()


def get_webhook_default_channels() -> list[str]:
    """新建监控规则时默认勾选的推送渠道 (多选)。

    空列表 = 新建规则默认不推送; ['feishu'] = 默认推飞书。
    此默认值供规则编辑器新建规则时预填, 单条规则仍可独立修改。

    向后兼容: 老版本只有布尔 webhook_enabled_default (勾选即飞书+企业微信双推),
    这里把 True 迁移为 ['feishu','wecom'], 还原当时的实际行为。
    """
    d = load()
    raw = d.get("webhook_default_channels")
    if isinstance(raw, list):
        return [c for c in raw if c in REVIEW_PUSH_CHANNELS]
    # 兼容老布尔开关 (勾选即双推)
    if d.get("webhook_enabled_default") is True:
        return ["feishu", "wecom"]
    return []


def set_webhook_default_channels(channels: list[str]) -> list[str]:
    """保存新建规则默认推送渠道 (多选)。过滤白名单外、去重、保序。空列表 = 不推送。"""
    seen: set[str] = set()
    cleaned: list[str] = []
    for c in channels or []:
        if c in REVIEW_PUSH_CHANNELS and c not in seen:
            seen.add(c)
            cleaned.append(c)
    save({"webhook_default_channels": cleaned})
    return cleaned


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
    if "minute_intraday_refresh" in cfg:
        updates["minute_intraday_refresh"] = bool(cfg["minute_intraday_refresh"])
    if "minute_intraday_refresh_interval" in cfg:
        # clamp 到 [5, 60], 与 getter 一致, 防前端传越界值
        updates["minute_intraday_refresh_interval"] = max(
            _INTRADAY_REFRESH_INTERVAL_MIN,
            min(_INTRADAY_REFRESH_INTERVAL_MAX, int(cfg["minute_intraday_refresh_interval"])))
    if "monitor_ext_fields" in cfg:
        raw = cfg["monitor_ext_fields"] or {}
        updates["monitor_ext_fields"] = {
            "concept": _normalize_ext_field(raw.get("concept")),
            "industry": _normalize_ext_field(raw.get("industry")),
        }
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
        "minute_intraday_refresh": get_minute_intraday_refresh(),
        "minute_intraday_refresh_interval": get_minute_intraday_refresh_interval(),
        "monitor_ext_fields": get_monitor_ext_fields(),
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
