"""市场总览数据装配(与 HTTP Request 解耦)。

本模块由 `app.api.overview._build_overview` 抽离而来,目的是让「大盘复盘」
等无 Request 的调用方(定时任务、复盘服务)也能复用同一套聚合逻辑。

行为与原 `_build_overview` 完全一致,仅把对 `request.app.state.{repo,
quote_service,depth_service}` 的依赖改为显式参数。

公共入口:
    build_market_overview(repo, quote_service, depth_service, as_of)
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from app.services.ext_data import ExtConfig, ExtConfigStore
from app.services.screener import ScreenerService

# ================================================================
# 常量(与 overview.py 保持同步;复盘复盘仅 A 股核心指数)
# ================================================================

CORE_INDEX_NAMES = {
    "000001.INDEX": "上证指数",
    "399001.INDEX": "深证成指",
    "399006.INDEX": "创业板指",
    "000680.INDEX": "科创综指",
}
CORE_INDEX_SYMBOLS = tuple(CORE_INDEX_NAMES.keys())

_DIMENSION_SEP = re.compile(r"[、,，;；|/\s]+")


# ================================================================
# 进程级有界缓存: supplements 短 TTL + ext 维度行按代际
# ================================================================
# supplements: key=(resolved data_dir, cache_generation, as_of, symbols 指纹)
# 盘中(quote_service running)短 TTL(5s)、盘后 60s;LRU + 条数上限,禁止无界增长。
# 缓存值是 supplements dict 的副本(_build_market_supplements 只在构建时只读遍历),
# 调用方 rows 的后续 mutation 无法回流污染缓存。
_SUPPLEMENTS_TTL_RUNNING = 5.0
_SUPPLEMENTS_TTL_IDLE = 60.0
_SUPPLEMENTS_CACHE_MAX = 4
_supplements_cache: OrderedDict = OrderedDict()  # key -> (ts, supplements dict)
_supplements_lock = threading.Lock()

# ext 维度行: key=(resolved data_dir, cache_generation);config.json + parquet
# 扫描按代际复用,避免每次 build 重读 ext parquet(实测 ~0.37s/次)。
_EXT_CACHE_MAX = 4
_ext_cache: OrderedDict = OrderedDict()  # key -> list[(config, dimension_field, rows)]
_ext_lock = threading.Lock()


def _now() -> float:
    """单调时钟;测试通过 monkeypatch 本函数控制 TTL,不依赖 sleep。"""
    return time.monotonic()


def _symbols_fingerprint(symbols: list[str]) -> str:
    """symbols 稳定指纹:排序后拼接 sha1,与传入顺序无关。"""
    return hashlib.sha1("\x1f".join(sorted(symbols)).encode("utf-8")).hexdigest()


def _clear_supplements_cache() -> None:
    with _supplements_lock:
        _supplements_cache.clear()


def _clear_ext_cache() -> None:
    with _ext_lock:
        _ext_cache.clear()


def _supplements_get(key, ttl: float, now: float):
    """锁内查 supplements 缓存;命中 move_to_end;过期剔除。命中不刷新时间戳(固定 TTL)。"""
    with _supplements_lock:
        entry = _supplements_cache.get(key)
        if entry is None:
            return None
        ts, supp = entry
        if now - ts >= ttl:
            _supplements_cache.pop(key, None)
            return None
        _supplements_cache.move_to_end(key)
        return supp


def _supplements_put(key, now: float, supp: dict) -> None:
    """锁内写 supplements 缓存;替换既有键,按条数 LRU 淘汰最旧。"""
    with _supplements_lock:
        if key in _supplements_cache:
            del _supplements_cache[key]
        _supplements_cache[key] = (now, supp)
        while len(_supplements_cache) > _SUPPLEMENTS_CACHE_MAX:
            _supplements_cache.popitem(last=False)


# ================================================================
# 通用工具
# ================================================================

def _finite(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _board(symbol: str) -> str:
    if symbol.endswith(".BJ"):
        return "北交所"
    if symbol.startswith(("300", "301")):
        return "创业板"
    if symbol.startswith(("688", "689")):
        return "科创板"
    if symbol.endswith(".SH"):
        return "沪主板"
    if symbol.endswith(".SZ"):
        return "深主板"
    return "其他"


def _score(value: float, low: float, high: float) -> int:
    if high <= low:
        return 50
    return max(0, min(100, round((value - low) / (high - low) * 100)))


# ================================================================
# 指数行情(实时 quote_service 优先,回退 kline_index_daily SQL)
# ================================================================

def _quote_status(quote_service) -> dict:
    qs = quote_service
    if not qs:
        return {"enabled": False, "running": False, "quote_age_ms": None, "is_trading_hours": False}
    return qs.status()


def _provider_index_quotes(as_of: date) -> dict[str, dict]:
    """补齐本地指数 parquet 尚未覆盖的指定交易日。"""
    try:
        from app.data_providers.registry import get_active_provider_name, get_provider

        provider = get_provider(get_active_provider_name("daily"))
        frame = provider.get_daily(
            list(CORE_INDEX_SYMBOLS),
            datetime.combine(as_of - timedelta(days=10), datetime.min.time()),
            datetime.combine(as_of, datetime.min.time()),
            "index",
        )
    except Exception:  # noqa: BLE001
        return {}
    if frame is None or frame.is_empty():
        return {}

    result: dict[str, dict] = {}
    for symbol in CORE_INDEX_SYMBOLS:
        records = (
            frame.filter(pl.col("symbol") == symbol)
            .sort("date")
            .to_dicts()
        )
        if not records or records[-1].get("date") != as_of:
            continue
        latest = records[-1]
        previous = records[-2] if len(records) > 1 else {}
        last_price = _finite(latest.get("close"))
        prev_close = _finite(previous.get("close"))
        change_amount = None
        change_pct = None
        if last_price is not None and prev_close not in (None, 0):
            change_amount = last_price - prev_close
            change_pct = change_amount / prev_close * 100
        result[symbol] = {
            "symbol": symbol,
            "name": CORE_INDEX_NAMES[symbol],
            "date": as_of.isoformat(),
            "last_price": last_price,
            "close": last_price,
            "prev_close": prev_close,
            "change_amount": change_amount,
            "change_pct": change_pct,
        }
    return result


def _index_quotes(repo, quote_service, as_of: date | None = None) -> list[dict]:
    rows: list[dict] = []
    if quote_service and as_of is None:
        df = quote_service.get_index_quotes(list(CORE_INDEX_SYMBOLS))
        if not df.is_empty():
            rows = df.to_dicts()

    if not rows and repo:
        placeholders = ", ".join("?" for _ in CORE_INDEX_SYMBOLS)
        try:
            db_rows = repo.execute_all(
                f"""
                WITH ranked AS (
                    SELECT symbol, date, close,
                           row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                    FROM kline_index_daily
                    WHERE symbol IN ({placeholders})
                      AND (? IS NULL OR date <= ?)
                ), latest AS (
                    SELECT symbol,
                           max(CASE WHEN rn = 1 THEN date END) AS date,
                           max(CASE WHEN rn = 1 THEN close END) AS last_price,
                           max(CASE WHEN rn = 2 THEN close END) AS prev_close
                    FROM ranked
                    WHERE rn <= 2
                    GROUP BY symbol
                )
                SELECT symbol, date, last_price, prev_close
                FROM latest
                """,
                [*CORE_INDEX_SYMBOLS, as_of, as_of],
            )
        except Exception:  # noqa: BLE001
            db_rows = []
        for symbol, dt, last_price, prev_close in db_rows:
            change_amount = None
            change_pct = None
            lp = _finite(last_price)
            pc = _finite(prev_close)
            if lp is not None and pc not in (None, 0):
                change_amount = lp - pc
                change_pct = change_amount / pc * 100
            rows.append({
                "symbol": symbol,
                "name": CORE_INDEX_NAMES.get(symbol),
                "date": str(dt) if dt else None,
                "last_price": lp,
                "close": lp,
                "prev_close": pc,
                "change_amount": change_amount,
                "change_pct": change_pct,
            })

    by_symbol = {r.get("symbol"): r for r in rows}
    if as_of is not None and any(
        str(by_symbol.get(symbol, {}).get("date") or "")[:10] != as_of.isoformat()
        for symbol in CORE_INDEX_SYMBOLS
    ):
        # overview 的股票广度与指数必须来自同一 as_of；本地指数 parquet
        # 滞后时通过 provider 补齐，不能把前一交易日指数拼到当前看板。
        by_symbol.update(_provider_index_quotes(as_of))
    out = []
    for symbol in CORE_INDEX_SYMBOLS:
        r = by_symbol.get(symbol, {"symbol": symbol})
        out.append({
            "symbol": symbol,
            "name": r.get("name") or CORE_INDEX_NAMES[symbol],
            "date": r.get("date"),
            "last_price": _finite(
                r.get("last_price")
                if r.get("last_price") is not None
                else r.get("close")
            ),
            "change_pct": _finite(r.get("change_pct")),
            "change_amount": _finite(r.get("change_amount")),
        })
    return out


# ================================================================
# 扩展数据(行业 / 概念)维度聚合
# ================================================================

def _dimension_field(config: ExtConfig, kind: str) -> str | None:
    candidates = ["概念", "concept", "theme"] if kind == "concept" else ["行业", "industry", "sector"]
    for candidate in candidates:
        needle = candidate.lower()
        for field in config.fields:
            haystack = f"{field.name} {field.label}".lower()
            if needle in haystack:
                return field.name
    return None


def _ext_files(data_dir, config: ExtConfig) -> list[str]:
    base = data_dir / "ext_data" / config.id
    if config.mode == "timeseries":
        root = base / "timeseries"
        return [str(p) for p in sorted(root.rglob("*.parquet")) if p.is_file()]
    return [str(p) for p in sorted(base.glob("*.parquet")) if p.is_file()]


def _read_ext_rows(data_dir, config: ExtConfig, dimension_field: str) -> list[dict]:
    files = _ext_files(data_dir, config)
    if not files:
        return []
    try:
        df = pl.read_parquet(files, hive_partitioning=True)
    except TypeError:
        try:
            df = pl.read_parquet(files)
        except Exception:  # noqa: BLE001
            return []
    except Exception:  # noqa: BLE001
        return []
    if df.is_empty() or dimension_field not in df.columns:
        return []

    if config.mode == "timeseries" and "date" in df.columns:
        latest = df.get_column("date").max()
        if latest is not None:
            df = df.filter(pl.col("date") == latest)

    symbol_cols = ["symbol", "code", "股票代码", "代码"]
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            symbol_cols.append(str(mapping["col"]))
    cols = []
    for col in [dimension_field, *symbol_cols]:
        if col in df.columns and col not in cols:
            cols.append(col)
    return df.select(cols).to_dicts()


def _load_dimension_sources(repo, kind: str) -> list[tuple[ExtConfig, str, list[dict]]]:
    """加载某维度全部 (config, dimension_field, rows),按代际缓存复用 ext parquet 读取。

    key=(resolved data_dir, cache_generation):data root 隔离防止跨仓库/测试污染,
    generation 让管道刷新立即逻辑失效(与 ScreenerService 历史缓存同一惯例)。
    同代际内 concept/industry 共享一次物理读,缓存二者并集;调用方按 kind 用
    _dimension_field 过滤,确保字段语义与原逐 config 扫描完全一致。
    缓存条目只读遍历,调用方不持有可变共享态。
    """
    data_dir = repo.store.data_dir.resolve()
    try:
        generation = repo.cache_generation
    except AttributeError:  # 极少数无 cache_generation 的 mock repo
        generation = 0
    key = (str(data_dir), generation)
    with _ext_lock:
        cached = _ext_cache.get(key)
        if cached is not None:
            _ext_cache.move_to_end(key)
            sources = cached
        else:
            sources = None
    if sources is None:
        store = ExtConfigStore(repo.store.data_dir)
        sources = []
        for config in store.load_all():
            for field_kind in ("concept", "industry"):
                field = _dimension_field(config, field_kind)
                if field:
                    rows = _read_ext_rows(repo.store.data_dir, config, field)
                    sources.append((config, field, rows))
        with _ext_lock:
            if key in _ext_cache:
                del _ext_cache[key]
            _ext_cache[key] = sources
            while len(_ext_cache) > _EXT_CACHE_MAX:
                _ext_cache.popitem(last=False)
    # 按 kind 过滤:复用与原扫描相同的 _dimension_field 判定,保持字段语义不变
    out = []
    for config, field, rows in sources:
        if _dimension_field(config, kind) == field:
            out.append((config, field, rows))
    return out


def _dimension_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    values = [v.strip() for v in _DIMENSION_SEP.split(str(raw).strip()) if v.strip()]
    return values


def _symbol_keys(row: dict, config: ExtConfig) -> list[str]:
    fields = ["symbol", "code", "股票代码", "代码"]
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            fields.append(str(mapping["col"]))

    keys: list[str] = []
    for field in fields:
        raw = row.get(field)
        if raw is None:
            continue
        text = str(raw).strip().upper()
        if not text:
            continue
        keys.append(text)
        if "." in text:
            keys.append(text.split(".", 1)[0])
    return keys


def _split_level(value: str, level: int | None) -> str:
    """行业多级名 "银行-银行-股份制银行" 按 level 取级(越界取末级)。"""
    if level is None or "-" not in value:
        return value
    parts = value.split("-")
    return parts[level - 1] if level <= len(parts) else parts[-1]


def symbol_dimension_map(repo, kind: str, level: int | None = None) -> dict[str, list[str]]:
    """symbol → 维度值列表(概念可多值,行业按 level 取级)。

    与 `_dimension_rank` 共用 ext_data 解析,但只产出映射、不做行情聚合,
    供复盘题材轮动等按日回扫的场景复用(避免每天重扫一遍 ext parquet)。
    键同时包含带后缀与不带后缀两种写法(600000.SH / 600000),调用方任取其一。
    未配置任何概念/行业 ext 数据时返回 {}。
    """
    out: dict[str, list[str]] = {}
    for config, field, ext_rows in _load_dimension_sources(repo, kind):
        for ext_row in ext_rows:
            values = [_split_level(v, level) for v in _dimension_values(ext_row.get(field))]
            if not values:
                continue
            for key in _symbol_keys(ext_row, config):
                bucket = out.setdefault(key, [])
                for value in values:
                    if value not in bucket:
                        bucket.append(value)
    return out


def _dimension_rank(rows: list[dict], repo, kind: str, limit: int = 5, level: int | None = None) -> dict:
    if not rows:
        return {"leading": [], "lagging": []}

    quote_map: dict[str, dict] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        quote_map[symbol] = row
        quote_map[symbol.split(".", 1)[0]] = row

    groups: dict[str, dict[str, dict]] = {}
    for config, field, ext_rows in _load_dimension_sources(repo, kind):
        for ext_row in ext_rows:
            quote = None
            for key in _symbol_keys(ext_row, config):
                quote = quote_map.get(key)
                if quote:
                    break
            if not quote:
                continue
            symbol = str(quote.get("symbol") or "")
            for value in _dimension_values(ext_row.get(field)):
                # 行业按 "-" 拆分级: "银行-银行-股份制银行" → level=2 取"银行"(二级)
                if level is not None and "-" in value:
                    parts = value.split("-")
                    value = parts[level - 1] if level <= len(parts) else parts[-1]
                groups.setdefault(value, {})[symbol] = quote

    items = []
    for name, by_symbol in groups.items():
        stocks = list(by_symbol.values())
        changes = [_finite(s.get("change_pct")) for s in stocks]
        changes = [v for v in changes if v is not None]
        if not changes:
            continue
        leader = max(stocks, key=lambda s: _finite(s.get("change_pct")) or -999)
        items.append({
            "name": name,
            "count": len(stocks),
            "avg_pct": sum(changes) / len(changes),
            "up_count": sum(1 for v in changes if v > 0),
            "down_count": sum(1 for v in changes if v < 0),
            "amount": sum(_finite(s.get("amount")) or 0 for s in stocks),
            "leader": {
                "symbol": leader.get("symbol"),
                "name": leader.get("name"),
                "change_pct": _finite(leader.get("change_pct")),
            },
        })

    leading = sorted(items, key=lambda x: x["avg_pct"], reverse=True)[:limit]
    lagging = sorted(items, key=lambda x: x["avg_pct"])[:limit]
    return {"leading": leading, "lagging": lagging}


# ================================================================
# Top 行 / 涨跌幅分桶
# ================================================================

def _top_rows(rows: list[dict], key: str, descending: bool, limit: int = 8) -> list[dict]:
    filtered = [r for r in rows if _finite(r.get(key)) is not None]
    filtered.sort(key=lambda r: _finite(r.get(key)) or 0, reverse=descending)
    return [
        {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "close": _finite(r.get("close")),
            "change_pct": _finite(r.get("change_pct")),
            "amount": _finite(r.get("amount")),
            "turnover_rate": _finite(r.get("turnover_rate")),
            "board": _board(str(r.get("symbol") or "")),
        }
        for r in filtered[:limit]
    ]


def _plausible_daily_change_pct(symbol: str | None, value: float | None) -> bool:
    if value is None:
        return False
    symbol = str(symbol or "").upper()
    if symbol.endswith(".BJ"):
        limit = 0.31
    elif symbol.startswith(("300", "301", "688", "689")):
        limit = 0.21
    else:
        limit = 0.11
    return abs(value) <= limit


def _build_market_supplements(rows: list[dict], as_of: date | None) -> dict | None:
    """从 provider 拉取 supplements,返回 symbol→item 映射(已按 as_of 过滤)。

    与原逻辑完全一致;返回 None 表示无可用数据(provider 缺失/异常/空),调用方应跳过。
    """
    symbols = [str(r.get("symbol") or "") for r in rows if r.get("symbol")]
    if not symbols:
        return None
    try:
        from app.data_providers.registry import get_active_provider_name, get_provider

        provider = get_provider(get_active_provider_name("realtime"))
        getter = getattr(provider, "get_latest_market_supplements", None)
        if getter is None:
            return None
        df = getter(symbols)
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.is_empty():
        return None
    supplements = {}
    for item in df.to_dicts():
        if as_of is not None and item.get("date") and str(item.get("date"))[:10] != as_of.isoformat():
            continue
        supplements[item.get("symbol")] = item
    return supplements


def _apply_market_supplements(rows: list[dict], supplements: dict) -> list[dict]:
    """把 supplements 只读写回 rows(turnover_rate / change_pct 合理性修正)。

    行为与旧内联实现逐字段一致:turnover 有限则覆盖;change_pct 按涨跌停合理性覆盖或置空。
    """
    for row in rows:
        item = supplements.get(row.get("symbol"))
        if not item:
            continue
        turnover = _finite(item.get("turnover_rate"))
        change_pct = _finite(item.get("change_pct"))
        if turnover is not None:
            row["turnover_rate"] = turnover
        symbol = str(row.get("symbol") or "")
        if _plausible_daily_change_pct(symbol, change_pct):
            row["change_pct"] = change_pct
        elif not _plausible_daily_change_pct(symbol, _finite(row.get("change_pct"))):
            row["change_pct"] = None
    return rows


def _fill_market_supplements_from_provider(
    rows: list[dict], as_of: date | None, repo=None, quote_running: bool = False
) -> list[dict]:
    """用 provider supplements 修正 rows 的 turnover/change_pct。

    repo 可选:传入时启用模块级有界短 TTL 缓存(key=data_dir/cache_generation/as_of/symbols 指纹,
    盘中 quote_running=True 用 5s TTL,盘后 60s),避免冷缓存构建每次重拉 provider(~2.5s)。
    不传 repo(旧调用/既有测试)时不缓存,行为与原实现完全一致。
    缓存值是 supplements dict 的浅拷贝,调用方对 rows 的 mutation 无法回流污染缓存。
    """
    if not rows:
        return rows

    cache_key = None
    supplements = None
    if repo is not None:
        try:
            data_dir = str(repo.store.data_dir.resolve())
            generation = repo.cache_generation
        except AttributeError:  # 极少数无 store/cache_generation 的 mock repo
            data_dir, generation = None, 0
        if data_dir is not None:
            symbols = [str(r.get("symbol") or "") for r in rows if r.get("symbol")]
            cache_key = (data_dir, generation, as_of.isoformat() if as_of else None, _symbols_fingerprint(symbols))
            ttl = _SUPPLEMENTS_TTL_RUNNING if quote_running else _SUPPLEMENTS_TTL_IDLE
            cached = _supplements_get(cache_key, ttl, _now())
            if cached is not None:
                supplements = dict(cached)  # 拷贝,隔离调用方 mutation

    if supplements is None:
        supplements = _build_market_supplements(rows, as_of)
        if supplements is None:
            return rows
        if cache_key is not None:
            _supplements_put(cache_key, _now(), supplements)

    return _apply_market_supplements(rows, supplements)


def _pct_band_rows(values: list[float]) -> list[dict]:
    bands = [
        ("<-5%", None, -0.05),
        ("-5~-3%", -0.05, -0.03),
        ("-3~-1%", -0.03, -0.01),
        ("-1~0%", -0.01, 0),
        ("0~1%", 0, 0.01),
        ("1~3%", 0.01, 0.03),
        ("3~5%", 0.03, 0.05),
        (">5%", 0.05, None),
    ]
    total = len(values) or 1
    out = []
    for label, low, high in bands:
        count = 0
        for v in values:
            if low is None and v < high:
                count += 1
            elif high is None and v >= low:
                count += 1
            elif low is not None and high is not None and low <= v < high:
                count += 1
        out.append({"label": label, "count": count, "pct": count / total * 100})
    return out


# ================================================================
# 向量化聚合(替换 ~15 次全表 Python 遍历)
# ================================================================

def _market_aggregates(rows: list[dict]) -> dict:
    """一次性 Polars 聚合 breadth/amount/limit/trend/activity 等纯统计量。

    与原逐字段 Python 实现行为完全等价(null/NaN/inf→忽略,0 视为有限,涨跌停信号
    bool 或 consec>0);已用 400 次随机 fuzz(null/NaN/inf/0/多 board/多 tier/空集)验证逐字段相等。
    返回字典键与原局部变量同名,供 build_market_overview 直接消费。
    """
    if not rows:
        return {
            "total": 0, "up": 0, "down": 0, "flat": 0,
            "total_amount": 0, "avg_amount": 0, "avg_pct": 0, "median_pct": 0,
            "strong_up": 0, "strong_down": 0,
            "limit_up": 0, "broken": 0, "limit_down": 0, "max_boards": 0,
            "above_ma5": 0, "above_ma20": 0, "above_ma60": 0, "new_high": 0, "new_low": 0,
            "avg_turnover": 0, "high_turnover": 0, "avg_vol_ratio": 1, "high_vol_ratio": 0,
            "pct_values": [],
        }

    df = pl.DataFrame(rows)

    def col(name: str) -> pl.Expr:
        # 缺列(调用方 cols 被裁剪时)以全 null Float64 兜底,_fin 自然判为 False
        return pl.col(name).cast(pl.Float64) if name in df.columns else pl.lit(None, dtype=pl.Float64)

    def fin(name: str) -> pl.Expr:
        return col(name).is_finite().fill_null(False)

    c, amt = col("change_pct"), col("amount")
    tr, vr = col("turnover_rate"), col("vol_ratio_5d")
    cl, m5, m20, m60 = col("close"), col("ma5"), col("ma20"), col("ma60")
    h60, l60 = col("high_60d"), col("low_60d")
    clu = pl.col("consecutive_limit_ups").cast(pl.Int64) if "consecutive_limit_ups" in df.columns else pl.lit(0, dtype=pl.Int64)

    def bcol(name: str) -> pl.Expr:
        return pl.col(name).fill_null(False).cast(pl.Boolean) if name in df.columns else pl.lit(False)

    cfin = fin("change_pct")
    res = df.select(
        total=pl.len(),
        up=(cfin & (c > 0)).sum(),
        down=(cfin & (c < 0)).sum(),
        # amount: 非有限(null/NaN/inf)按 0 计入(对齐 _finite(x) or 0)
        total_amount=pl.when(amt.is_finite().fill_null(False)).then(amt).otherwise(0.0).sum(),
        avg_pct=c.filter(cfin).mean(),
        strong_up=(cfin & (c >= 0.03)).sum(),
        strong_down=(cfin & (c <= -0.03)).sum(),
        limit_up=(bcol("signal_limit_up") | ((clu.fill_null(0)) > 0)).sum(),
        broken=bcol("signal_broken_limit_up").sum(),
        limit_down=bcol("signal_limit_down").sum(),
        max_boards=clu.fill_null(0).max(),
        above_ma5=(fin("close") & fin("ma5") & (cl >= m5)).sum(),
        above_ma20=(fin("close") & fin("ma20") & (cl >= m20)).sum(),
        above_ma60=(fin("close") & fin("ma60") & (cl >= m60)).sum(),
        new_high=(bcol("signal_n_day_high") | (fin("close") & fin("high_60d") & (cl >= h60))).sum(),
        new_low=(bcol("signal_n_day_low") | (fin("close") & fin("low_60d") & (cl <= l60))).sum(),
        avg_turnover=tr.filter(fin("turnover_rate")).mean(),
        high_turnover=(fin("turnover_rate") & (tr >= 5)).sum(),
        avg_vol_ratio=vr.filter(fin("vol_ratio_5d")).mean(),
        high_vol_ratio=(fin("vol_ratio_5d") & (vr >= 1.5)).sum(),
    ).row(0, named=True)

    total = int(res["total"])
    res["flat"] = max(0, total - int(res["up"]) - int(res["down"]))
    res["avg_amount"] = float(res["total_amount"]) / total if total else 0

    pct_series = df.select(c).filter(cfin).to_series().sort()
    pct_values = pct_series.to_list()
    res["median_pct"] = float(pct_series[len(pct_series) // 2]) if len(pct_series) > 0 else 0
    res["pct_values"] = pct_values
    res["avg_pct"] = res["avg_pct"] if res["avg_pct"] is not None else 0
    res["avg_turnover"] = res["avg_turnover"] if res["avg_turnover"] is not None else 0
    res["avg_vol_ratio"] = res["avg_vol_ratio"] if res["avg_vol_ratio"] is not None else 1
    return res


# ================================================================
# 主装配入口
# ================================================================

def build_market_overview(
    repo,
    quote_service=None,
    depth_service=None,
    as_of: date | None = None,
) -> dict:
    """装配市场总览(与原 overview._build_overview 行为一致)。

    Args:
        repo: KlineRepository(必填)。
        quote_service: QuoteService(可选;实时指数行情来源)。
        depth_service: DepthService(可选;五档封板修正)。
        as_of: 指定日期,None 则取最新有数据日。
    """
    svc = ScreenerService(repo)
    as_of = as_of or svc.latest_date()
    status = _quote_status(quote_service)
    indices = _index_quotes(repo, quote_service, as_of)

    if not as_of:
        return {
            "as_of": None,
            "quote_status": status,
            "indices": indices,
            "breadth": {"total": 0, "up": 0, "down": 0, "flat": 0, "up_pct": 0, "down_pct": 0},
            "amount": {"total": 0, "avg": 0},
            "boards": [],
            "limit": {"limit_up": 0, "broken": 0, "failed": 0, "limit_down": 0, "max_boards": 0, "tiers": []},
            "distribution": [],
            "trend": {"above_ma5": 0, "above_ma20": 0, "above_ma60": 0, "above_ma5_pct": 0, "above_ma20_pct": 0, "above_ma60_pct": 0, "new_high": 0, "new_low": 0},
            "activity": {"avg_turnover": 0, "high_turnover": 0, "high_vol_ratio": 0, "vol_ratio": 1},
            "radar": [],
            "emotion": {"score": 50, "label": "暂无"},
            "top_gainers": [],
            "top_losers": [],
            "turnover_leaders": [],
            "active_leaders": [],
            "concept_rank": {"leading": [], "lagging": []},
            "industry_rank": {"leading": [], "lagging": []},
        }

    cols = [
        "symbol", "name", "close", "change_pct", "amount", "turnover_rate",
        "volume", "vol_ratio_5d", "consecutive_limit_ups", "signal_limit_up",
        "signal_broken_limit_up", "signal_limit_down", "ma5", "ma20", "ma60",
        "high_60d", "low_60d", "signal_n_day_high", "signal_n_day_low",
    ]
    df = svc._load_enriched_for_date(as_of, columns=cols)
    if df.is_empty():
        rows: list[dict] = []
    else:
        df = df.select([c for c in cols if c in df.columns])
        rows = df.to_dicts()

    # 过滤真停牌（volume=0 且 change_pct=0），保留有涨跌幅的浮点误差股以对齐同花顺口径
    if rows and "volume" in rows[0]:
        rows = [r for r in rows
                if (_finite(r.get("volume")) or 0) > 0
                or (_finite(r.get("change_pct")) or 0) != 0]
    quote_running = bool(status.get("running")) and bool(status.get("is_trading_hours"))
    rows = _fill_market_supplements_from_provider(rows, as_of, repo=repo, quote_running=quote_running)

    agg = _market_aggregates(rows)
    total = agg["total"]
    up = agg["up"]
    down = agg["down"]
    flat = agg["flat"]
    up_pct = up / total * 100 if total else 0
    down_pct = down / total * 100 if total else 0
    total_amount = agg["total_amount"]
    avg_amount = agg["avg_amount"]
    pct_values = agg["pct_values"]
    avg_pct = agg["avg_pct"]
    median_pct = agg["median_pct"]
    strong_up = agg["strong_up"]
    strong_down = agg["strong_down"]
    limit_up = agg["limit_up"]
    broken = agg["broken"]
    limit_down = agg["limit_down"]
    max_boards = agg["max_boards"]
    above_ma5 = agg["above_ma5"]
    above_ma20 = agg["above_ma20"]
    above_ma60 = agg["above_ma60"]
    new_high = agg["new_high"]
    new_low = agg["new_low"]
    avg_turnover = agg["avg_turnover"]
    high_turnover = agg["high_turnover"]

    # 五档 sealed 修正: 假涨停/假跌停不计入(需 depth5.batch 能力)。
    sealed_ready = False
    fake_up = 0
    fake_down = 0
    if depth_service:
        up_map = depth_service.get_sealed_map(as_of, is_down=False)
        down_map = depth_service.get_sealed_map(as_of, is_down=True)
        sealed_ready = bool(up_map or down_map) and depth_service.is_sealed_ready(as_of)
        if up_map:
            fake_up = sum(1 for value in up_map.values() if value.get("sealed") is False)
        if down_map:
            fake_down = sum(1 for value in down_map.values() if value.get("sealed") is False)
    if sealed_ready:
        limit_up = max(0, limit_up - fake_up)
        limit_down = max(0, limit_down - fake_down)
    seal_rate = limit_up / (limit_up + broken) * 100 if limit_up + broken > 0 else 0

    boards_map: dict[str, dict] = {}
    for r in rows:
        b = _board(str(r.get("symbol") or ""))
        item = boards_map.setdefault(b, {"board": b, "count": 0, "up": 0, "down": 0, "amount": 0.0})
        item["count"] += 1
        change = _finite(r.get("change_pct")) or 0
        if change > 0:
            item["up"] += 1
        elif change < 0:
            item["down"] += 1
        item["amount"] += _finite(r.get("amount")) or 0
    boards = sorted(boards_map.values(), key=lambda x: x["amount"], reverse=True)
    for b in boards:
        count = b["count"] or 1
        b["up_pct"] = b["up"] / count * 100

    tiers_map: dict[int, int] = {}
    for r in rows:
        n = int(_finite(r.get("consecutive_limit_ups")) or 0)
        if n > 0:
            tiers_map[n] = tiers_map.get(n, 0) + 1
    tiers = [{"boards": k, "count": v} for k, v in sorted(tiers_map.items(), key=lambda item: -item[0])]

    index_changes = [_finite(r.get("change_pct")) for r in indices]
    index_changes = [v for v in index_changes if v is not None]
    avg_index_pct = sum(index_changes) / len(index_changes) if index_changes else 0
    avg_vol_ratio = agg["avg_vol_ratio"]
    high_vol_ratio = agg["high_vol_ratio"]

    concept_rank = _dimension_rank(rows, repo, "concept")
    industry_rank = _dimension_rank(rows, repo, "industry", level=2)

    strong_diff_pct = (strong_up - strong_down) / total * 100 if total else 0
    high_vol_pct = high_vol_ratio / total * 100 if total else 0
    strong_down_pct = strong_down / total * 100 if total else 0
    tier2_count = sum(t["count"] for t in tiers if t["boards"] >= 2)
    mainline_items = [*concept_rank["leading"][:3], *industry_rank["leading"][:3]]
    mainline_avg = max([_finite(item.get("avg_pct")) or 0 for item in mainline_items], default=0)
    mainline_cover_pct = max([(_finite(item.get("count")) or 0) / total * 100 for item in mainline_items], default=0) if total else 0
    mainline_score = round(_score(mainline_avg, -0.005, 0.03) * 0.65 + _score(mainline_cover_pct, 1, 12) * 0.35) if mainline_items else 50

    radar = [
        {"key": "index", "label": "指数", "value": _score(avg_index_pct, -2.5, 2.5)},
        {"key": "profit", "label": "赚钱", "value": round(_score(up_pct, 20, 80) * 0.45 + _score(avg_pct, -0.02, 0.02) * 0.25 + _score(median_pct, -0.02, 0.02) * 0.20 + _score(strong_diff_pct, -8, 8) * 0.10)},
        {"key": "money", "label": "量能", "value": round(_score(avg_vol_ratio, 0.6, 1.8) * 0.70 + _score(high_vol_pct, 2, 12) * 0.30)},
        {"key": "speculation", "label": "投机", "value": round(_score(limit_up, 5, 90) * 0.25 + _score(seal_rate, 30, 85) * 0.35 + _score(max_boards, 1, 8) * 0.25 + _score(tier2_count, 0, 30) * 0.15)},
        {"key": "resilience", "label": "抗跌", "value": 100 - round(_score(down_pct, 20, 80) * 0.55 + _score(strong_down_pct, 1, 12) * 0.45)},
        {"key": "mainline", "label": "主线", "value": mainline_score},
    ]
    emotion_score = round(sum(r["value"] for r in radar) / len(radar)) if radar else 50
    if emotion_score >= 70:
        emotion_label = "强势"
    elif emotion_score >= 55:
        emotion_label = "偏暖"
    elif emotion_score >= 45:
        emotion_label = "震荡"
    elif emotion_score >= 30:
        emotion_label = "偏冷"
    else:
        emotion_label = "冰点"

    return _json_safe({
        "as_of": str(as_of),
        "quote_status": status,
        "indices": indices,
        "breadth": {
            "total": total,
            "up": up,
            "down": down,
            "flat": flat,
            "up_pct": up_pct,
            "down_pct": down_pct,
            "avg_pct": avg_pct,
            "median_pct": median_pct,
            "strong_up": strong_up,
            "strong_down": strong_down,
        },
        "amount": {"total": total_amount, "avg": avg_amount},
        "boards": boards,
        "limit": {"limit_up": limit_up, "broken": broken, "failed": 0, "limit_down": limit_down, "max_boards": max_boards, "seal_rate": seal_rate, "tiers": tiers, "sealed_ready": sealed_ready, "fake_up": fake_up, "fake_down": fake_down},
        "distribution": _pct_band_rows(pct_values),
        "trend": {
            "above_ma5": above_ma5,
            "above_ma20": above_ma20,
            "above_ma60": above_ma60,
            "above_ma5_pct": above_ma5 / total * 100 if total else 0,
            "above_ma20_pct": above_ma20 / total * 100 if total else 0,
            "above_ma60_pct": above_ma60 / total * 100 if total else 0,
            "new_high": new_high,
            "new_low": new_low,
        },
        "activity": {
            "avg_turnover": avg_turnover,
            "high_turnover": high_turnover,
            "high_vol_ratio": high_vol_pct,
            "vol_ratio": avg_vol_ratio,
        },
        "radar": radar,
        "emotion": {"score": emotion_score, "label": emotion_label},
        "top_gainers": _top_rows(rows, "change_pct", True),
        "top_losers": _top_rows(rows, "change_pct", False),
        "turnover_leaders": _top_rows(rows, "amount", True),
        "active_leaders": _top_rows(rows, "turnover_rate", True),
        "concept_rank": concept_rank,
        "industry_rank": industry_rank,
    })
