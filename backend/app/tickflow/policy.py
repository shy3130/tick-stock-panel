"""能力探测 + CapabilitySet 持久化(§5.3)。

探测策略:逐 capability 用最小代价请求试探。
  - 成功 → 记录可用,优先取响应头 X-RateLimit-* 否则用 tiers.yaml 默认
  - 抛权限错 → 不可用
  - 抛其他错 → 不可用(谨慎,保留日志)

Tier Label 算法见 §5.3:基线档 + 补丁能力。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.config import settings
from app import secrets_store

from .capabilities import Cap, CapabilityLimits, CapabilitySet

logger = logging.getLogger(__name__)

_CAPSET_CACHE_FILE = "capabilities.json"

# 缓存 schema 版本。capabilities 模型有结构性变更时 bump(如新增/拆分 Cap),
# 旧缓存(无此字段或版本更低)会被判定过期,触发重新探测。
# v2: 拆分 depth5 → depth5(单只) + depth5.batch(批量)
# v3: 探测补全 quote.batch(此前 tiers.yaml 声明了但 _probe_real 漏探测)
# v5: Free 档补充付费服务器 quote.by_symbol(10rpm/5标的),用于自选股实时监控。
_CACHE_SCHEMA_VERSION = 5

# 探测用最小代价请求:挑流通性最好的 1 只标的试
_PROBE_SYMBOL = "600000.SH"  # 浦发银行,长期不会退市


def _load_tiers_yaml() -> dict[str, dict[str, dict[str, Any]]]:
    for path in [settings.tiers_yaml, Path("/app/tiers.yaml"), Path("../tiers.yaml")]:
        if path.exists():
            with path.open(encoding="utf-8") as f:
                return yaml.safe_load(f)
    raise FileNotFoundError("tiers.yaml not found")


def _tier_to_capset(tier_def: dict[str, dict[str, Any]]) -> CapabilitySet:
    caps: dict[Cap, CapabilityLimits] = {}
    for cap_name, limits_dict in tier_def.items():
        try:
            cap = Cap(cap_name)
        except ValueError:
            logger.warning("unknown cap in tiers.yaml: %s", cap_name)
            continue
        caps[cap] = CapabilityLimits(
            rpm=limits_dict.get("rpm"),
            batch=limits_dict.get("batch"),
            subscribe=limits_dict.get("subscribe"),
        )
    return CapabilitySet(caps)


def _is_transient(e: Exception) -> bool:
    """是否为"可重试的瞬时错误"——网络抖动 / 限流 / 服务端 5xx。

    与权限/参数错误(403/401/400/404)区分:后者重试也无用,不重试。
    用类名匹配而非 import SDK 异常,避免探测期对 SDK 内部耦合。
    """
    cls = e.__class__.__name__
    if cls in {
        "RateLimitError", "InternalServerError", "APIError",
        "ConnectionError", "TimeoutError", "ConnectError",
        "ConnectTimeout", "ReadTimeout", "RemoteProtocolError",
        "httpx.ConnectError", "httpx.TimeoutException",
    }:
        return True
    # APIError 体系下,status_code 5xx/429 视为瞬时
    status = getattr(e, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    return False


def _call_with_retry(fn, attempts: int = 3, backoff: float = 0.6) -> None:
    """调用 fn();对瞬时错误退避重试,权限/参数错误立即抛出。

    attempts=总尝试次数(含首次)。返回 None,异常由调用方分类。
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            fn()
            return
        except Exception as e:  # noqa: BLE001
            last_exc = e
            # 权限/参数类错误:重试无意义,立即抛出交给 try_call 归类
            if not _is_transient(e):
                raise
            # 瞬时错误:最后一轮不再 sleep
            if i < attempts - 1:
                time.sleep(backoff * (i + 1))
    # 重试耗尽,抛出最后一次异常
    assert last_exc is not None
    raise last_exc


def _probe_real(tiers: dict) -> tuple[CapabilitySet, list[str], set[Cap]]:
    """逐 capability 试探。需要 API key。

    **关键**:探测始终在付费端点(api.tickflow.org)上进行,用 key 鉴权验证有效性。
    绝不能读旧 capabilities 缓存的档位来选服务器 —— 否则首次保存 key 时,
    旧缓存是 none 档 → get_client() 返回 free 服务器 → free 服务器忽略 key →
    乱填 key 也能拿到日K → 误判成 free 档(鸡生蛋蛋生鸡的循环依赖 bug)。

    返回 (capset, probe_log)。
    """
    from tickflow import TickFlow
    from .client import _base_url, PAID_ENDPOINT

    key = secrets_store.get_tickflow_key()
    # 探测专用客户端:强制走付费端点验证 key。
    # base_url 用用户自定义端点(若已配置测速切换),否则默认 api.tickflow.org。
    probe_base = _base_url() or PAID_ENDPOINT
    logger.info("开始能力探测 (付费端点=%s, SDK默认超时=30s×重试3)", probe_base)
    tf = TickFlow(api_key=key, base_url=probe_base)
    available: dict[Cap, CapabilityLimits] = {}
    log: list[str] = []
    # 重试耗尽仍失败的瞬时错误(非明确无权限)对应的 cap。供上层判定: 若「分水岭」
    # cap(单只日K/复权因子)是瞬时失败, 不要据此把付费用户降级为 free/none。
    transient_failed: set[Cap] = set()

    def try_call(cap: Cap, fn, default_limits: dict[str, Any]) -> None:
        # 分段耗时日志: 记录每个 cap 探测的开始/结束/耗时/结果, 便于定位卡死环节。
        _t0 = time.perf_counter()
        logger.info("能力探测开始: %s", cap.value)
        try:
            _call_with_retry(fn)
            available[cap] = CapabilityLimits(
                rpm=default_limits.get("rpm"),
                batch=default_limits.get("batch"),
                subscribe=default_limits.get("subscribe"),
            )
            _elapsed = time.perf_counter() - _t0
            log.append(f"✓ {cap}")
            logger.info("能力探测完成: %s ✓ (%.2fs)", cap.value, _elapsed)
        except Exception as e:  # noqa: BLE001
            _elapsed = time.perf_counter() - _t0
            msg = str(e).lower()
            cls = e.__class__.__name__
            # PermissionError 类名 / HTTP 403 / 中英文权限关键词都算"明确无权限"
            is_perm_denied = (
                cls in {"PermissionError", "AuthorizationError"}
                or "permission" in msg or "unauthorized" in msg
                or "403" in msg or "forbidden" in msg
                or "套餐" in msg or "权限" in msg or "需要" in msg
            )
            if is_perm_denied:
                log.append(f"✗ {cap}(无权限)")
                logger.info("能力探测完成: %s ✗ 无权限 (%.2fs)", cap.value, _elapsed)
            elif _is_transient(e):
                # 仅**真瞬时**错误(超时/连接/5xx/429, 由 _is_transient 判定)才标记为疑似 —
                # 与探测重试用同一判据。否则一个消息未命中权限关键词的确定性失败
                # (如 401/"authentication failed"/"key expired")会被误当瞬时, 让降级
                # 保护(保留旧付费档)反而掩盖真实的 Key 失效, 永不回落到 free-api。
                transient_failed.add(cap)
                log.append(f"? {cap} (瞬时: {cls}: {e})")
                logger.warning("能力探测瞬时失败: %s ? %s: %s (%.2fs)", cap.value, cls, e, _elapsed)
            else:
                # 非权限关键词、也非瞬时 → 视为该能力确实不可用(不保留、不重试保护)
                log.append(f"✗ {cap}({cls}: {e})")
                logger.info("能力探测完成: %s ✗ %s: %s (%.2fs)", cap.value, cls, e, _elapsed)

    # 用各档默认上限作为占位(无 X-RateLimit-* 头时)
    # 取所有档的并集,逐 cap 试探
    all_caps_defaults: dict[str, dict[str, Any]] = {}
    for tier in ("free", "starter", "pro", "expert"):
        for cap_name, lim in tiers.get(tier, {}).items():
            all_caps_defaults.setdefault(cap_name, lim)

    def defaults(cap: Cap) -> dict[str, Any]:
        return all_caps_defaults.get(str(cap), {})

    # 全部用 keyword-only 形式调用,符合 SDK 真实签名
    # quote.by_symbol
    try_call(Cap.QUOTE_BY_SYMBOL,
             lambda: tf.quotes.get(symbols=[_PROBE_SYMBOL], as_dataframe=False),
             defaults(Cap.QUOTE_BY_SYMBOL))

    # quote.batch — 批量行情(POST /v1/quotes)。用 get_by_symbols 试探。
    try_call(Cap.QUOTE_BATCH,
             lambda: tf.quotes.get_by_symbols([_PROBE_SYMBOL], as_dataframe=False),
             defaults(Cap.QUOTE_BATCH))

    # quote.pool — 用一个真实存在的 universe id 试探。
    # universes.list() 在 Free 也开放,先拿任意一个 universe id 再用 get_by_universes 试。
    def _probe_pool():
        unis = tf.universes.list()
        if not unis:
            raise RuntimeError("no universes available")
        first_id = unis[0]["id"] if isinstance(unis[0], dict) else getattr(unis[0], "id")
        return tf.quotes.get_by_universes([first_id], as_dataframe=False)

    try_call(Cap.QUOTE_POOL, _probe_pool, defaults(Cap.QUOTE_POOL))

    # kline.daily.by_symbol — Free 也有
    try_call(Cap.KLINE_DAILY_BY_SYMBOL,
             lambda: tf.klines.get(_PROBE_SYMBOL, period="1d", count=1, as_dataframe=False),
             defaults(Cap.KLINE_DAILY_BY_SYMBOL))

    # kline.daily.batch
    try_call(Cap.KLINE_DAILY_BATCH,
             lambda: tf.klines.batch([_PROBE_SYMBOL], period="1d", count=1, as_dataframe=False),
             defaults(Cap.KLINE_DAILY_BATCH))

    # kline.minute.by_symbol
    try_call(Cap.KLINE_MINUTE_BY_SYMBOL,
             lambda: tf.klines.get(_PROBE_SYMBOL, period="1m", count=1, as_dataframe=False),
             defaults(Cap.KLINE_MINUTE_BY_SYMBOL))

    # kline.minute.batch
    try_call(Cap.KLINE_MINUTE_BATCH,
             lambda: tf.klines.batch([_PROBE_SYMBOL], period="1m", count=1, as_dataframe=False),
             defaults(Cap.KLINE_MINUTE_BATCH))

    # intraday
    try_call(Cap.INTRADAY,
             lambda: tf.klines.intraday(_PROBE_SYMBOL, count=1, as_dataframe=False),
             defaults(Cap.INTRADAY))

    # intraday.batch
    try_call(Cap.INTRADAY_BATCH,
             lambda: tf.klines.intraday_batch([_PROBE_SYMBOL], count=1, as_dataframe=False),
             defaults(Cap.INTRADAY_BATCH))

    # depth5 — 按标的查(单只)
    try_call(Cap.DEPTH5,
             lambda: tf.depth.get(_PROBE_SYMBOL),
             defaults(Cap.DEPTH5))

    # depth5.batch — 批量查(SDK 0.1.23+ 提供 depth.batch,对应官方 /v1/depth/batch 端点)
    try_call(Cap.DEPTH5_BATCH,
             lambda: tf.depth.batch([_PROBE_SYMBOL]),
             defaults(Cap.DEPTH5_BATCH))

    # financial — SDK 提供 income / balance_sheet / cash_flow / metrics / shares
    # 用 metrics 探测(单据最小)
    try_call(Cap.FINANCIAL,
             lambda: tf.financials.metrics([_PROBE_SYMBOL], latest=True, as_dataframe=False),
             defaults(Cap.FINANCIAL))

    # adj_factor — 实际在 klines.ex_factors
    try_call(Cap.ADJ_FACTOR,
             lambda: tf.klines.ex_factors([_PROBE_SYMBOL], as_dataframe=False),
             defaults(Cap.ADJ_FACTOR))

    # websocket 不在探测期试连接(成本太高且阻塞),按档位默认推断
    # 若 expert 的其他 cap 都通,则推断 websocket 也可用
    if (Cap.FINANCIAL in available and Cap.INTRADAY_BATCH in available):
        available[Cap.WEBSOCKET] = CapabilityLimits(
            subscribe=defaults(Cap.WEBSOCKET).get("subscribe", 100),
        )
        log.append("✓ websocket (inferred from expert tier)")

    return CapabilitySet(available), log, transient_failed


def _load_cached_capset(cache_path: Path) -> CapabilitySet | None:
    """读取上次持久化的 capset(schema 匹配时)。供瞬时失败时保留旧档位用。

    此时尚未 _persist 本次探测结果, 缓存文件仍是上一次的值。schema 不匹配则返回 None
    (旧结构不可靠, 不作为保留依据)。
    """
    try:
        if not cache_path.exists():
            return None
        with cache_path.open(encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("schema_version") != _CACHE_SCHEMA_VERSION:
            return None
        return _capset_from_json(cached)
    except Exception:  # noqa: BLE001
        return None


def detect_capabilities(force: bool = False) -> CapabilitySet:
    """探测当前可用的能力集 (TickFlow API Key 档位 + 自定义数据源)。

    自定义数据源补能力: 用户配了自定义分钟数据源时, 即使无 TickFlow Pro+
    也补上 KLINE_MINUTE_BATCH, 使分时图/自动同步/回测等功能不再被权限门拦。
    取数函数内部会按 preferences.get_minute_data_provider() 分流到自定义源,
    不会错误调用 TickFlow。
    """
    capset = _detect_tickflow_caps(force)
    _augment_custom_sources(capset)
    return capset


def _augment_custom_sources(capset: CapabilitySet) -> None:
    """根据用户配置的自定义数据源, 补充对应能力 (不覆盖 TickFlow 已有的)。"""
    try:
        from app.services import preferences
        provider = preferences.get_minute_data_provider()
        if provider != "tickflow":
            from app.data_providers import custom as custom_sources
            if custom_sources.provider_has_dataset(provider, "minute"):
                capset.grant(Cap.KLINE_MINUTE_BATCH)
                logger.info("custom minute source '%s' detected: granted KLINE_MINUTE_BATCH", provider)
    except Exception as e:  # noqa: BLE001
        logger.debug("custom source augment skipped: %s", e)


def _detect_tickflow_caps(force: bool = False) -> CapabilitySet:
    """探测 TickFlow API Key 的能力集 (不含自定义数据源补能力)。"""
    cache_path = settings.data_dir / _CAPSET_CACHE_FILE
    if not force and cache_path.exists():
        with cache_path.open(encoding="utf-8") as f:
            cached = json.load(f)
        # schema 版本校验:旧缓存或缺版本号 → 过期,丢弃后重新探测
        if cached.get("schema_version") == _CACHE_SCHEMA_VERSION:
            return _capset_from_json(cached)
        logger.info("capabilities 缓存 schema 版本过期(缓存=%s, 当前=%d), 重新探测",
                    cached.get("schema_version"), _CACHE_SCHEMA_VERSION)

    tiers = _load_tiers_yaml()
    if settings.use_free_mode:
        # 无 key —— 归 none 档(走 free-api 服务器,仅历史日K)
        capset = _tier_to_capset(tiers["none"])
        _persist(capset, "None", log=["无 API Key(无档 · free-api 服务器)"], missing=[], extras=[])
        return capset

    # 有 API key — 真实探测
    try:
        _probe_t0 = time.perf_counter()
        capset, probe_log, transient_failed = _probe_real(tiers)
        logger.info("能力探测全部完成, 总耗时 %.2fs", time.perf_counter() - _probe_t0)
        # 判定档位:无效 key → none,免费 key → free,付费 → starter/pro/expert
        classified = _classify_tier(capset, tiers)

        # 瞬时探测失败不得触发降级: 分水岭 cap(单只日K / 复权因子)本次是瞬时失败
        # (非明确无权限), 且此前缓存过付费档(有复权因子)时, 保留旧缓存档位、不持久化
        # 降级。否则一次网络抖动就把付费用户误降为 free/none, 直到强制重探才恢复。
        prev_capset = _load_cached_capset(cache_path)
        prev_was_paid = prev_capset is not None and Cap.ADJ_FACTOR in prev_capset.all()
        transient_downgrade = (
            (classified.is_invalid and Cap.KLINE_DAILY_BY_SYMBOL in transient_failed)
            or (classified.is_free and Cap.ADJ_FACTOR in transient_failed)
        )
        if prev_was_paid and transient_downgrade:
            logger.warning(
                "能力探测分水岭瞬时失败(非无权限): %s; 保留上次缓存档位, 不降级",
                sorted(str(c) for c in transient_failed),
            )
            return prev_capset

        if classified.is_invalid:
            # 无效 key(连单只日K都拿不到):归 none 档,标记要求清除 key
            capset = _tier_to_capset(tiers["none"])
            probe_log.append("⚠ Key 无效(单只日K也无法获取),判定为无档")
            _persist(capset, "None", log=probe_log, missing=[], extras=[], invalid_key=True)
            return capset
        if classified.is_free:
            # 免费有效 key:按 free 档能力持久化(日K free-api + 按标的实时)。
            capset = _tier_to_capset(tiers["free"])
            _persist(capset, "Free", log=probe_log + ["✓ 免费有效 key(运行时走 free-api 服务器)"], missing=[], extras=[])
            return capset
        # 付费档(starter+) — 探测出的能力即为真实可用
        label, missing, extras = _compute_label_and_missing(capset, tiers)
        capset = _override_limits_with_detected_tier(capset, label, tiers)
        _persist(capset, label, log=probe_log, missing=missing, extras=extras)
        return capset
    except Exception as e:
        logger.exception("detect_capabilities failed; using none baseline: %s", e)
        capset = _tier_to_capset(tiers["none"])
        _persist(capset, "None(探测失败)", log=[f"探测失败:{e}"], missing=[], extras=[])
        return capset


# ===== Tier 代表性 capability(signature caps)=====
# 拥有**任意一个**即认作该档及以上。自上而下匹配。
# 这套设计的好处:单个 capability 探测的 transient 失败不会把整体档位"误降"。
TIER_SIGNATURES: dict[str, set[Cap]] = {
    "expert":  {Cap.FINANCIAL, Cap.INTRADAY_BATCH, Cap.WEBSOCKET},
    "pro":     {Cap.KLINE_MINUTE_BATCH, Cap.KLINE_MINUTE_BY_SYMBOL,
                Cap.INTRADAY, Cap.DEPTH5, Cap.DEPTH5_BATCH},
    "starter": {Cap.QUOTE_BATCH, Cap.KLINE_DAILY_BATCH,
                Cap.ADJ_FACTOR, Cap.QUOTE_POOL},
    # free / none 不需 signature — 由 _classify_tier 的分水岭逻辑判定
}


@dataclass(slots=True, frozen=True)
class TierClassification:
    """档位判定结果。

    判定依据是"复权因子分水岭":
      - 连单只日K都没有 → 无效 key(is_invalid),归 none 档
      - 有单只日K、无复权因子 → 免费 key(is_free)
      - 有复权因子 → 付费档(starter+),具体档位由 signature 决定
    """

    tier: str            # "none" / "free" / "starter" / "pro" / "expert"
    is_invalid: bool     # 无效 key(连单只日K都拿不到)
    is_free: bool        # 免费有效 key(有日K、无复权因子)


def _classify_tier(capset: CapabilitySet, tiers: dict) -> TierClassification:
    """根据探测出的能力集判定档位。

    分水岭是 KLINE_DAILY_BY_SYMBOL(单只日K)与 ADJ_FACTOR(复权因子):
      - 无单只日K     → none(无效 key)
      - 有日K无复权   → free(免费 key)
      - 有复权因子    → 走 signature 判定 starter/pro/expert
    """
    held = set(capset.all().keys())

    # 1) 连单只日K都没有 → 无效 key
    if Cap.KLINE_DAILY_BY_SYMBOL not in held:
        return TierClassification(tier="none", is_invalid=True, is_free=False)

    # 2) 有日K但无复权因子 → 免费 key
    if Cap.ADJ_FACTOR not in held:
        return TierClassification(tier="free", is_invalid=False, is_free=True)

    # 3) 有复权因子 → 付费档,按 signature 自上而下判定
    if held & TIER_SIGNATURES["expert"]:
        base = "expert"
    elif held & TIER_SIGNATURES["pro"]:
        base = "pro"
    elif held & TIER_SIGNATURES["starter"]:
        base = "starter"
    else:
        # 有复权因子但无任何代表能力 — 兜底为 starter(复权本身是 starter 特征)
        base = "starter"
    return TierClassification(tier=base, is_invalid=False, is_free=False)

# 补丁友好命名(label 后缀用)
_CAP_ALIASES: dict[Cap, str] = {
    Cap.KLINE_MINUTE_BATCH: "分钟K",
    Cap.KLINE_MINUTE_BY_SYMBOL: "分钟K",
    Cap.INTRADAY: "分时",
    Cap.INTRADAY_BATCH: "批量分时",
    Cap.DEPTH5: "五档",
    Cap.DEPTH5_BATCH: "批量五档",
    Cap.WEBSOCKET: "WS",
    Cap.FINANCIAL: "财务",
    Cap.ADJ_FACTOR: "复权",
    Cap.QUOTE_BATCH: "批量行情",
    Cap.QUOTE_POOL: "标的池",
    Cap.KLINE_DAILY_BATCH: "日K批量",
}


def _override_limits_with_detected_tier(
    capset: CapabilitySet, label: str, tiers: dict,
) -> CapabilitySet:
    """探测完成后,用判档对应的 limits 覆盖每个 cap 的速率/批量。

    判档前每个 cap 用的是"所有档默认值的并集"(为了不漏数据),
    判档后才知道用户真实档位,limits 用该档的实际值更准。
    label 可能是 "Pro" / "Pro + 分钟K" / "Pro+" 等组合形式 — 取第一个词当作基线档名。
    """
    base_name = label.split()[0].split("+")[0].strip().lower()  # "Pro + 分钟K" → "pro"
    tier_limits = tiers.get(base_name, {})
    new_caps: dict[Cap, CapabilityLimits] = {}
    for cap, _old_lim in capset.all().items():
        spec = tier_limits.get(cap.value)
        if spec:
            new_caps[cap] = CapabilityLimits(
                rpm=spec.get("rpm"),
                batch=spec.get("batch"),
                subscribe=spec.get("subscribe"),
            )
        else:
            # 不在该档定义里(extras),用 expert 档兜底(最宽松)
            expert_spec = tiers.get("expert", {}).get(cap.value, {})
            new_caps[cap] = CapabilityLimits(
                rpm=expert_spec.get("rpm"),
                batch=expert_spec.get("batch"),
                subscribe=expert_spec.get("subscribe"),
            )
    return CapabilitySet(new_caps)


def _tier_caps_set(tiers: dict, tier_name: str) -> set[Cap]:
    """读 tiers.yaml 的某档定义,转为 Cap 集合。"""
    return {Cap(c) for c in tiers.get(tier_name, {}).keys() if c in {x.value for x in Cap}}


def _compute_label_and_missing(
    capset: CapabilitySet, tiers: dict,
) -> tuple[str, list[str], list[str]]:
    """返回 (label, missing_caps, extra_caps)。

    label:档位标签。
    missing_caps:本档**应有但未探测到**的 capability(用于诊断:可能是探测 bug 或权限丢失)。
    extra_caps:超出本档的额外 capability(自定义组合)。
    """
    held = set(capset.all().keys())

    # 1) 完全匹配 — 干净命中某档
    for tier_name in ["free", "starter", "pro", "expert"]:
        if held == _tier_caps_set(tiers, tier_name):
            return tier_name.capitalize(), [], []

    # 2) 按 signature 自上而下判档
    if held & TIER_SIGNATURES["expert"]:
        base = "expert"
    elif held & TIER_SIGNATURES["pro"]:
        base = "pro"
    elif held & TIER_SIGNATURES["starter"]:
        base = "starter"
    else:
        base = "free"

    base_caps = _tier_caps_set(tiers, base)
    missing = sorted(c.value for c in (base_caps - held))
    extras = base_caps and (held - base_caps) or set()  # extras 是超出该档的部分

    # 实际超出 = held 中"既不属于本档、也不属于本档下方任何档"的 cap
    # 简化:extras = held - base_caps
    extras_set = held - base_caps

    # 3) 拼 label
    if not extras_set:
        # 完全在本档内(可能缺一两项 — 由 missing 反映)
        return base.capitalize(), missing, []

    # 补丁过多 → 用 "≈" 形式
    if len(extras_set) > 3:
        return f"{base.capitalize()}+", missing, sorted(c.value for c in extras_set)

    suffix = sorted({_CAP_ALIASES.get(e, str(e)) for e in extras_set})
    return f"{base.capitalize()} + " + " + ".join(suffix), missing, sorted(c.value for c in extras_set)


def _compute_label(capset: CapabilitySet, tiers: dict) -> str:
    """对外简化签名 — 只要 label。"""
    label, _missing, _extras = _compute_label_and_missing(capset, tiers)
    return label


def _persist(
    capset: CapabilitySet,
    label: str,
    log: list[str] | None = None,
    missing: list[str] | None = None,
    extras: list[str] | None = None,
    invalid_key: bool = False,
) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    cache_path = settings.data_dir / _CAPSET_CACHE_FILE
    payload = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "label": label,
        "capabilities": capset.to_dict(),
        "probe_log": log or [],
        "missing_caps": missing or [],   # 本档应有但未探测到
        "extras_caps": extras or [],     # 超出本档的额外能力
        "invalid_key": invalid_key,      # 探测出的 key 无效(连单只日K都拿不到)
    }
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _capset_from_json(data: dict[str, Any]) -> CapabilitySet:
    caps: dict[Cap, CapabilityLimits] = {}
    for cap_name, lim in data.get("capabilities", {}).items():
        try:
            cap = Cap(cap_name)
        except ValueError:
            continue
        caps[cap] = CapabilityLimits(
            rpm=lim.get("rpm"),
            batch=lim.get("batch"),
            subscribe=lim.get("subscribe"),
        )
    return CapabilitySet(caps)


def tier_label() -> str:
    cache_path = settings.data_dir / _CAPSET_CACHE_FILE
    if cache_path.exists():
        with cache_path.open(encoding="utf-8") as f:
            return json.load(f).get("label", "Unknown")
    return "Unknown"


def probe_log() -> list[str]:
    cache_path = settings.data_dir / _CAPSET_CACHE_FILE
    if cache_path.exists():
        with cache_path.open(encoding="utf-8") as f:
            return json.load(f).get("probe_log", [])
    return []


def missing_caps() -> list[str]:
    """本档应有但未探测到的 capability — 通常意味着探测有 bug 或权限边界。"""
    cache_path = settings.data_dir / _CAPSET_CACHE_FILE
    if cache_path.exists():
        with cache_path.open(encoding="utf-8") as f:
            return json.load(f).get("missing_caps", [])
    return []


def extras_caps() -> list[str]:
    cache_path = settings.data_dir / _CAPSET_CACHE_FILE
    if cache_path.exists():
        with cache_path.open(encoding="utf-8") as f:
            return json.load(f).get("extras_caps", [])
    return []


def is_invalid_key() -> bool:
    """最近一次探测是否判定 key 无效(连单只日K都拿不到)。

    settings 层据此清除已存的 key,避免乱填的 key 被持久化。
    """
    cache_path = settings.data_dir / _CAPSET_CACHE_FILE
    if cache_path.exists():
        with cache_path.open(encoding="utf-8") as f:
            return bool(json.load(f).get("invalid_key", False))
    return False


def base_tier_name() -> str:
    """当前档位的基础名(小写): none / free / starter / pro / expert。

    供 client 层判断"是否走 free-api 服务器"(none/free → free 服务器)。
    """
    label = tier_label()
    return label.split()[0].split("+")[0].strip().lower()
