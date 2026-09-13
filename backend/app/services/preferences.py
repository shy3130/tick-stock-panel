"""用户偏好设置持久化。

存储位置: data/user_data/preferences.json
沿用 secrets_store 的 merge-write 模式,但不做 chmod 0600 (非敏感数据)。
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# 进程内缓存: 行情轮询线程一轮会调用 8~12 次 getter, 每次读盘+parse 是纯重复;
# 文件仅在用户改设置时变化, 以 (mtime_ns, size) 签名判断是否重读。
_cache: dict | None = None
_cache_sig: tuple[int, int] | None = None


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "preferences.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _invalidate_cache() -> None:
    global _cache, _cache_sig
    _cache = None
    _cache_sig = None


def load() -> dict:
    """读取 preferences.json (带 mtime 签名缓存)。返回深拷贝, 调用方可自由修改。"""
    global _cache, _cache_sig
    p = _path()
    try:
        sig = (p.stat().st_mtime_ns, p.stat().st_size)
    except OSError:
        return {}
    if _cache is not None and sig == _cache_sig:
        return copy.deepcopy(_cache)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("preferences.json malformed: %s", e)
        return {}
    _cache = data
    _cache_sig = sig
    return copy.deepcopy(_cache)


_SAVE_LOCK = threading.Lock()


def save(updates: dict) -> dict:
    """合并写入。返回新内容。

    锁内 read-modify-write: FastAPI 同步端点跑线程池, 并行 PUT 各自基于旧快照
    写盘会互相覆盖 (实测: 压缩总开关并行写分时/日K两键, 后写者把先写者覆盖)。
    """
    with _SAVE_LOCK:
        current = load()
        current.update(updates)
        atomic_write_text(
            _path(), json.dumps(current, indent=2, ensure_ascii=False),
        )
        _invalidate_cache()
    return current


def get_realtime_quotes_enabled() -> bool:
    return load().get("realtime_quotes_enabled", False)


def get_watchlist_groups_in_nav() -> bool:
    """自选分组是否显示在侧边栏（可展开二级子菜单）。默认 False。"""
    return load().get("watchlist_groups_in_nav", False)


def get_realtime_quote_interval() -> float:
    return load().get("realtime_quote_interval", 6.0)


def set_realtime_quote_interval(interval: float) -> float:
    """保存行情轮询间隔（不在此做 min/max 校验，由调用方按档位限制）。

    走 save() 而不是自己 load + write_text: 锁外的 read-modify-write 会用旧快照
    整体覆盖文件, 把并发写入的另一个偏好丢掉 (见 save 的 docstring)。
    """
    save({"realtime_quote_interval": interval})
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

# ===== 盘中分钟增量刷新 (Expert 专有) =====

# 稳态轮为 intraday.universe 单请求增量, 无脉冲并发, 间隔可低至 3s;
# 全天修复轮 (intraday.batch 28 块爆发) 的 rpm 安全与间隔无关, 由轮次
# 调度 max(间隔, 单轮完成) 天然防重叠。
_MINUTE_REFRESH_INTERVAL_MIN = 3
_MINUTE_REFRESH_INTERVAL_MAX = 120


def get_minute_refresh_enabled() -> bool:
    """盘中分钟K增量落盘开关。默认关闭; 能力门控 (Expert) 在服务层判断。"""
    return bool(load().get("minute_refresh_enabled", False))


def get_minute_refresh_interval() -> int:
    """盘中分钟增量刷新间隔(秒)。默认 6,范围 [3, 120]。"""
    return max(
        _MINUTE_REFRESH_INTERVAL_MIN,
        min(_MINUTE_REFRESH_INTERVAL_MAX, int(load().get("minute_refresh_interval", 6))),
    )


# ===== 数据源选择 (默认 TickFlow；第一阶段仅日K切换入口) =====

_ALLOWED_DATA_PROVIDERS = {"tickflow"}
DATA_SOURCE_JOB_TIMEOUT_MIN_S = 60


def get_data_source_job_timeout_s() -> int:
    """返回普通数据后台任务的卡死判定时间(秒)。"""
    from app.services.pipeline_jobs import DEFAULT_JOB_TIMEOUT_S
    raw = load().get("data_source_job_timeout_s", DEFAULT_JOB_TIMEOUT_S)
    try:
        timeout_s = int(raw)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_JOB_TIMEOUT_S
    return max(DATA_SOURCE_JOB_TIMEOUT_MIN_S, timeout_s)


def get_data_source_long_job_timeout_s() -> int:
    """返回分钟 K 全市场等长任务的卡死判定时间(秒)。"""
    from app.services.pipeline_jobs import LONG_JOB_TIMEOUT_S
    raw = load().get(
        "data_source_long_job_timeout_s",
        LONG_JOB_TIMEOUT_S,
    )
    try:
        timeout_s = int(raw)
    except (TypeError, ValueError):
        timeout_s = LONG_JOB_TIMEOUT_S
    return max(DATA_SOURCE_JOB_TIMEOUT_MIN_S, timeout_s)


def get_minute_batch_compress() -> bool:
    """分时详情与批量响应是否启用 gzip 传输压缩。默认开启 (公网部署传输是大头);
    本机/内网可关闭省服务端 CPU。每次请求即时读取, 开关保存后立即生效。
    """
    raw = load().get("minute_batch_compress", True)
    return bool(raw)


def get_daily_batch_compress() -> bool:
    """日K详情与批量响应是否启用 gzip 传输压缩 (与分时各自独立配置)。默认开启。"""
    raw = load().get("daily_batch_compress", True)
    return bool(raw)


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
    # 「跟随日K」(same_as_daily) 特殊值已下线: 存量配置里的旧值按非法值回退 tickflow
    provider = str(load().get("adj_factor_provider", "tickflow") or "tickflow").lower()
    return provider if provider in _allowed_data_providers() else "tickflow"


def get_minute_data_provider() -> str:
    provider = str(load().get("minute_data_provider", "tickflow") or "tickflow").lower()
    return provider if provider in _allowed_data_providers() else "tickflow"


def get_full_minute_data_provider() -> str:
    provider = str(load().get("full_minute_data_provider", "tickflow") or "tickflow").lower()
    return provider if provider in _allowed_data_providers() else "tickflow"


def get_depth5_data_provider() -> str:
    provider = str(load().get("depth5_data_provider", "tickflow") or "tickflow").lower()
    return provider if provider in _allowed_data_providers() else "tickflow"


def get_realtime_data_provider() -> str:
    provider = str(load().get("realtime_data_provider", "tickflow") or "tickflow").lower()
    return provider if provider in _allowed_data_providers() else "tickflow"


def get_financial_provider() -> str:
    provider = str(load().get("financial_data_provider", "tickflow") or "tickflow").lower()
    return provider if provider in _allowed_data_providers() else "tickflow"


# ===== 盘后管道拉取内容开关 (A股 / ETF / 指数 独立控制) =====

def get_pipeline_pull_a_share() -> bool:
    """是否拉取 A 股日K。默认 True。"""
    return load().get("pipeline_pull_a_share", True)


def get_pipeline_pull_etf() -> bool:
    """是否拉取 ETF 日K。默认 False(标的多,首次较慢)。"""
    return load().get("pipeline_pull_etf", False)


def get_pipeline_pull_index() -> bool:
    """是否拉取指数日K。默认 True。"""
    return load().get("pipeline_pull_index", True)


def get_pipeline_regime_enabled() -> bool:
    """盘后管道是否自动计算市场环境(regime)。默认 False。

    regime 是本地聚合计算(非拉取), 首次/regime 表为空时需全量回填多日,
    内存与耗时较高, 故默认关闭; 用户可在数据页「市场环境」卡片设置里开启,
    或直接在该页面点「重算」手动触发(不受此开关影响)。
    """
    return load().get("pipeline_regime_enabled", False)


# regime 全量回填分批参数范围:
# - batch_days: 每批目标交易日数。越小内存越省、批次越多越慢; ma20 需 20 交易日,
#   故下限 25(留 warmup 余量), 上限 500(约 2 年)。
# - warmup_days: 每批前缀预热天数(日历日), 必须 > ma20 的 20 交易日(≈28 日历日),
#   下限 35 留余量, 上限 90。
_REGIME_BATCH_DAYS_MIN = 25
_REGIME_BATCH_DAYS_MAX = 500
_REGIME_WARMUP_DAYS_MIN = 35
_REGIME_WARMUP_DAYS_MAX = 90


def get_regime_batch_days() -> int:
    """regime 全量回填每批目标交易日数。默认 60(约一季度)。

    超过此天数的范围会被切成多批, 每批独立算指标后拼接, 控制内存峰值。
    """
    v = load().get("regime_batch_days", 60)
    try:
        return max(_REGIME_BATCH_DAYS_MIN, min(_REGIME_BATCH_DAYS_MAX, int(v)))
    except (TypeError, ValueError):
        return 60


def get_regime_warmup_days() -> int:
    """regime 分批每批的 warmup 前缀日历天数。默认 40。

    用于预热 ma20 等滚动窗口指标, 使每批边界计算正确。必须 > 20 交易日。
    """
    v = load().get("regime_warmup_days", 40)
    try:
        return max(_REGIME_WARMUP_DAYS_MIN, min(_REGIME_WARMUP_DAYS_MAX, int(v)))
    except (TypeError, ValueError):
        return 40


# ── 市场主线(概念/行业涨停梯队)过滤 ──
# 宽基/风格标签(融资融券 ~7700 成分、深股通/沪股通 ~3300-3700、国企改革 ~2900)
# 会按"家数"霸占主线榜首, 但它们不是可操作的题材主线。默认按成分股数上限过滤。
# 标定(2026-08 THS 概念): 成员 >600 的 55 个概念几乎全是此类风格标签,
# 真实题材(华为概念 2006/人工智能 2166/固态电池等)均在 600 以下或可自行调整。
_MAINLINE_MAX_MEMBERS_MIN = 50
_MAINLINE_MAX_MEMBERS_MAX = 5000
_MAINLINE_MIN_MEMBERS_MIN = 1
_MAINLINE_MIN_MEMBERS_MAX = 200


def get_mainline_max_members() -> int:
    """主线维度成员数上限, 超过视为宽基/风格标签被过滤。默认 600。"""
    v = load().get("mainline_max_members", 600)
    try:
        return max(_MAINLINE_MAX_MEMBERS_MIN, min(_MAINLINE_MAX_MEMBERS_MAX, int(v)))
    except (TypeError, ValueError):
        return 600


def get_mainline_min_members() -> int:
    """主线维度成员数下限, 过滤微型标签。默认 4。"""
    v = load().get("mainline_min_members", 4)
    try:
        return max(_MAINLINE_MIN_MEMBERS_MIN, min(_MAINLINE_MIN_MEMBERS_MAX, int(v)))
    except (TypeError, ValueError):
        return 4


def get_mainline_blacklist() -> list[str]:
    """用户自定义屏蔽的维度成员名(不论成员数大小)。默认空。

    保存时接受 list 或逗号/顿号/分号/空白分隔的字符串。
    """
    v = load().get("mainline_blacklist", [])
    if isinstance(v, str):
        v = [part for part in re.split(r"[,，、;；\s]+", v) if part]  # noqa: RUF001
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def get_sentiment_exclude_st() -> bool:
    """市场环境/主线统计是否剔除风险警示(ST)股。默认 True。

    口径: 主板 ST 在 2026-07 前享 5% 涨跌幅(封板成本减半), 且 ST 是跨行业的
    状态桶而非投资题材, 混入会系统性抬高涨停宽度/高度(弱市尤甚)。剔除后
    涨跌家数等宽度占比几乎不受影响。修改后需重算 regime 与主线生效。
    """
    return bool(load().get("sentiment_exclude_st", True))


def set_sentiment_exclude_st(v: bool) -> bool:
    save({"sentiment_exclude_st": bool(v)})
    return get_sentiment_exclude_st()


def get_mainline_filter_config() -> dict:
    """主线过滤配置汇总(供 API 返回与计算读取)。"""
    return {
        "min_members": get_mainline_min_members(),
        "max_members": get_mainline_max_members(),
        "blacklist": get_mainline_blacklist(),
        "exclude_st": get_sentiment_exclude_st(),
    }


def set_mainline_filter_config(cfg: dict) -> dict:
    """保存主线过滤配置(白名单字段, 部分更新)。修改后需重算主线生效。"""
    updates: dict = {}
    if "min_members" in cfg and cfg["min_members"] is not None:
        updates["mainline_min_members"] = cfg["min_members"]
    if "max_members" in cfg and cfg["max_members"] is not None:
        updates["mainline_max_members"] = cfg["max_members"]
    if "exclude_st" in cfg and cfg["exclude_st"] is not None:
        updates["sentiment_exclude_st"] = bool(cfg["exclude_st"])
    if "blacklist" in cfg and cfg["blacklist"] is not None:
        raw = cfg["blacklist"]
        if isinstance(raw, str):
            raw = [part for part in re.split(r"[,，、;；\s]+", raw) if part]  # noqa: RUF001
        updates["mainline_blacklist"] = [str(x).strip() for x in (raw or []) if str(x).strip()]
    if updates:
        save(updates)
    return get_mainline_filter_config()


_PIPELINE_PULL_KEYS = ("pipeline_pull_a_share", "pipeline_pull_etf", "pipeline_pull_index")


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
    """返回盘后管道调度时间 {"hour": 15, "minute": 35}。

    默认 15:35 而非 15:30 整: 盘后固定价交易 15:30 才彻底结束, 且供应商
    聚合含盘后量的官方日K需要时间 —— 整点即拉可能写入不含盘后成交的
    日线, 也与 quote 定版重试窗口终点 (15:30) 精确重合。留 5 分钟缓冲。
    """
    d = load().get("pipeline_schedule", {"hour": 15, "minute": 35})
    return {"hour": d.get("hour", 15), "minute": d.get("minute", 35)}


def set_pipeline_schedule(hour: int, minute: int) -> dict:
    h = max(0, min(23, hour))
    m = max(0, min(59, minute))
    # 盘后管道不早于 15:35: 15:30 盘后固定价才终止 (量/额此前仍会变),
    # 且供应商官方日线定稿需要缓冲 —— 更早启动可能固化不含盘后量的当日分区
    if h * 60 + m < 15 * 60 + 35:
        h, m = 15, 35
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


# 监控与复盘共用的外部推送渠道白名单。
# 多选: 不推送 = 空数组, 而非 'none'
PUSH_CHANNELS = {"feishu", "wecom", "custom", "email"}


def get_review_schedule() -> dict:
    """定时复盘调度 {"enabled": False, "hour": 15, "minute": 40}。默认关闭。

    默认 15:40: 盘后管道默认 15:35 启动, 留 5 分钟缓冲, 复盘使用管道
    产出的最终口径数据 (含盘后量校正的日K/enriched)。强制下限 15:00 —
    偏好收盘后即时复盘 (走实时快照缓存, 不等管道) 的用户可自行调早。
    """
    d = load().get("review_schedule", {"enabled": False, "hour": 15, "minute": 40})
    return {
        "enabled": bool(d.get("enabled", False)),
        "hour": d.get("hour", 15),
        "minute": d.get("minute", 40),
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


MINING_BUDGET_PROFILES = frozenset({"balanced", "strict"})


def get_mining_schedule() -> dict:
    """返回周度自动 mining 配置。历史配置缺字段时默认关闭。"""
    data = load()
    weekday = data.get("mining_schedule_weekday", 4)
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 4:
        weekday = 4
    profile = data.get("mining_budget_profile", "balanced")
    if not isinstance(profile, str) or profile not in MINING_BUDGET_PROFILES:
        profile = "balanced"
    enabled = data.get("mining_schedule_enabled", False)
    if not isinstance(enabled, bool):
        enabled = False
    return {
        "mining_schedule_enabled": enabled,
        "mining_schedule_weekday": weekday,
        "mining_budget_profile": profile,
    }


def set_mining_schedule(enabled: bool, weekday: int, profile: str) -> dict:
    """校验并一次写入周度自动 mining 的整组配置。"""
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 4:
        raise ValueError("mining schedule weekday must be between 0 and 4")
    if profile not in MINING_BUDGET_PROFILES:
        raise ValueError("mining budget profile must be balanced or strict")
    result = {
        "mining_schedule_enabled": bool(enabled),
        "mining_schedule_weekday": weekday,
        "mining_budget_profile": profile,
    }
    save(result)
    return result


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
        return [c for c in raw if c in PUSH_CHANNELS]
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
        if c in PUSH_CHANNELS and c not in seen:
            seen.add(c)
            cleaned.append(c)
    save({"review_push_channels": cleaned})
    return cleaned


REVIEW_PUSH_MODES = frozenset({"auto", "manual"})


def get_review_push_mode() -> str:
    """复盘推送触发方式: auto=归档后自动推; manual=仅显式 push。默认 manual。

    定时复盘与手动保存复盘共用此开关。manual 时定时路径只归档不推送,
    手动路径需 save_report 显式传 push=True 才推。
    """
    mode = load().get("review_push_mode", "manual")
    return mode if mode in REVIEW_PUSH_MODES else "manual"


def set_review_push_mode(mode: str) -> str:
    """保存复盘推送触发方式, 白名单外的值回退 manual。"""
    mode = mode if mode in REVIEW_PUSH_MODES else "manual"
    save({"review_push_mode": mode})
    return mode



# ===== 实时监控 =====

# 页面 SSE 刷新配置: { "watchlist": true, "monitor": true, ... }
# 可刷新的页面列表及其默认值
SSE_REFRESH_PAGES_DEFAULT = {
    "watchlist": True,
    "limit-ladder": False,
}


# ===== 盘中实时行情范围 (独立于盘后管道范围) =====
# 指数不在其中: 展示层固定核心四只 (app.services.index_const), 不开放配置。


def get_realtime_pull_stock() -> bool:
    return load().get("realtime_pull_stock", True)


def get_realtime_pull_etf() -> bool:
    # 老用户兼容: ETF 实时默认关闭，避免升级后请求量/写盘量突然增加。
    return load().get("realtime_pull_etf", False)


def set_realtime_quote_scope(cfg: dict) -> dict:
    updates = {}
    for key in ("realtime_pull_stock", "realtime_pull_etf"):
        if key in cfg and cfg[key] is not None:
            updates[key] = bool(cfg[key])
    if updates:
        save(updates)
    return get_realtime_quote_scope()


def get_realtime_quote_scope() -> dict:
    return {
        "realtime_pull_stock": get_realtime_pull_stock(),
        "realtime_pull_etf": get_realtime_pull_etf(),
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


def get_custom_webhook_url() -> str:
    """Generic third-party JSON Webhook URL shared by enabled rules and reviews."""
    return str(load().get("custom_webhook_url") or "")


def set_custom_webhook_url(url: str) -> str:
    """Persist or clear the generic third-party JSON Webhook URL."""
    value = str(url or "").strip()
    save({"custom_webhook_url": value})
    return value


_EMAIL_SMTP_DEFAULTS = {
    "host": "",
    "port": 465,
    "security": "ssl",
    "username": "",
    "from_address": "",
    "to_addresses": [],
}


def get_email_smtp_config() -> dict:
    """Return non-secret SMTP settings for the email notification channel."""
    raw = load().get("email_smtp_config")
    if not isinstance(raw, dict):
        raw = {}
    security = raw.get("security", _EMAIL_SMTP_DEFAULTS["security"])
    if security not in {"ssl", "starttls", "none"}:
        security = _EMAIL_SMTP_DEFAULTS["security"]
    try:
        port = int(raw.get("port", _EMAIL_SMTP_DEFAULTS["port"]))
    except (TypeError, ValueError):
        port = _EMAIL_SMTP_DEFAULTS["port"]
    if not 1 <= port <= 65535:
        port = _EMAIL_SMTP_DEFAULTS["port"]
    recipients = raw.get("to_addresses")
    if not isinstance(recipients, list):
        recipients = []
    return {
        "host": str(raw.get("host") or "").strip(),
        "port": port,
        "security": security,
        "username": str(raw.get("username") or "").strip(),
        "from_address": str(raw.get("from_address") or "").strip(),
        "to_addresses": [str(item).strip() for item in recipients if str(item).strip()],
    }


def set_email_smtp_config(config: dict) -> dict:
    """Atomically persist the non-secret SMTP configuration group."""
    normalized = {
        "host": str(config.get("host") or "").strip(),
        "port": int(config.get("port", 465)),
        "security": str(config.get("security") or "ssl"),
        "username": str(config.get("username") or "").strip(),
        "from_address": str(config.get("from_address") or "").strip(),
        "to_addresses": [
            str(item).strip() for item in config.get("to_addresses", []) if str(item).strip()
        ],
    }
    save({"email_smtp_config": normalized})
    return get_email_smtp_config()


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
        return [c for c in raw if c in PUSH_CHANNELS]
    # 兼容老布尔开关 (勾选即双推)
    if d.get("webhook_enabled_default") is True:
        return ["feishu", "wecom"]
    return []


def set_webhook_default_channels(channels: list[str]) -> list[str]:
    """保存新建规则默认推送渠道 (多选)。过滤白名单外、去重、保序。空列表 = 不推送。"""
    seen: set[str] = set()
    cleaned: list[str] = []
    for c in channels or []:
        if c in PUSH_CHANNELS and c not in seen:
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
    if "screener_auto_run" in cfg:
        updates["screener_auto_run"] = bool(cfg["screener_auto_run"])
    if "minute_intraday_refresh" in cfg:
        updates["minute_intraday_refresh"] = bool(cfg["minute_intraday_refresh"])
    if "minute_intraday_refresh_interval" in cfg:
        # clamp 到 [5, 60], 与 getter 一致, 防前端传越界值
        updates["minute_intraday_refresh_interval"] = max(
            _INTRADAY_REFRESH_INTERVAL_MIN,
            min(_INTRADAY_REFRESH_INTERVAL_MAX, int(cfg["minute_intraday_refresh_interval"])))
    if "minute_refresh_enabled" in cfg:
        updates["minute_refresh_enabled"] = bool(cfg["minute_refresh_enabled"])
    if "minute_refresh_interval" in cfg:
        # clamp 到 [3, 120], 与 getter 一致, 防前端传越界值
        updates["minute_refresh_interval"] = max(
            _MINUTE_REFRESH_INTERVAL_MIN,
            min(_MINUTE_REFRESH_INTERVAL_MAX, int(cfg["minute_refresh_interval"])))
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
        "screener_auto_run": get_screener_auto_run(),
        "minute_intraday_refresh": get_minute_intraday_refresh(),
        "minute_intraday_refresh_interval": get_minute_intraday_refresh_interval(),
        "minute_refresh_enabled": get_minute_refresh_enabled(),
        "minute_refresh_interval": get_minute_refresh_interval(),
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
