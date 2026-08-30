"""eltdx 客户端生命周期管理 + 数据获取薄封装。

eltdx 是进程内 TCP 长连接池(Rust Runtime 驱动), 与 stock-sdk 的 subprocess 桥接不同:
整个后端进程复用同一个 TdxClient, close() 时统一释放。

本模块同时把 eltdx 的 dataclass 返回统一转成 list[dict] / list[str],
使 provider 只消费纯 Python 结构, 便于隔离测试(monkeypatch 本模块函数即可,
无需真实 eltdx 与网络)。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from datetime import time as dtime

logger = logging.getLogger(__name__)

# 全市场代码表 / 名称映射的进程内缓存有效期(秒)。代码表一天内基本不变。
_CACHE_TTL = 86400

_client = None
_lock = threading.Lock()
_a_share_codes_cache: tuple[float, list[str]] | None = None
_code_names_cache: tuple[float, dict[str, str]] | None = None
_share_capitals_cache: tuple[float, dict[str, dict]] | None = None

# 批量快照单批代码数(协议单批上限未文档化, 保守取值)。
SNAPSHOT_BATCH = 80
# 财务快照单批代码数(与 SDK helpers._finance_map 一致)。
_FINANCE_BATCH = 80
# 代码表分页上限, 防死循环(单市场代码表远小于此值)。
_MAX_CODE_PAGES = 40
_CODE_PAGE_SIZE = 1600

# K 线分页参数: bars.get 单页根数(协议上限内)与增量分页最大页数(分钟全量 24001 根 ≈ 31 页)。
_BAR_PAGE_SIZE = 800
_MAX_BAR_PAGES = 100

_MARKETS = (0, 1, 2)  # 0=深, 1=沪, 2=北


class EltdxBridgeError(RuntimeError):
    """eltdx 调用失败(依赖缺失 / 连接异常 / 返回非法)。"""


def get_client():
    """惰性创建并复用单例 TdxClient(连接池: 默认 2 主站 x 4 TCP)。

    首次请求时才测速建连, 失败由调用方捕获后降级。
    """
    global _client
    with _lock:
        if _client is None:
            from eltdx import TdxClient

            _client = TdxClient(timeout=5)
        return _client


def close_client() -> None:
    """释放 socket 与 Rust Runtime。loader.load_all 重建注册表时会调 provider.close()。"""
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                logger.debug("eltdx client close failed", exc_info=True)
            _client = None


def _to_jsonable(obj):
    """把 eltdx dataclass/模型转成纯 Python 结构, 转不了就原样返回。"""
    if obj is None or isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    try:
        from eltdx import to_jsonable
    except ImportError:
        return obj
    return to_jsonable(obj)


def availability() -> tuple[bool, str]:
    """探活: 返回 (是否可用, 原因)。不抛异常。

    用独立的临时 TdxClient 做连通性检测, 不触碰进程级单例 _client:
    若复用单例, 失败后的 close_client() 会关闭后台实时/同步线程正在使用的共享连接池。
    """
    try:
        import eltdx
    except ImportError:
        return False, "未安装 eltdx, 运行: pip install eltdx"
    client = None
    try:
        client = eltdx.TdxClient(timeout=5)
        client.codes.count(market=0)
        return True, f"ok (eltdx {eltdx.__version__})"
    except Exception as e:
        return False, f"eltdx 连通性检测失败: {e}"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug("eltdx probe client close failed", exc_info=True)


def bars_all(code: str, period: str, since: datetime | date | None = None) -> list[dict]:
    """拉取标的 K 线, 返回按时间升序的 bar dict 列表。

    bar dict 含: time(ISO 含 +08:00) / open / high / low / close /
    volume_wire_value(股, 由 provider 转手) / amount(元)。

    since 非 None 时只拉 since(含) 之后的数据: 用 bars.get 按最新在前分页,
    页内升序、页码越大时间越早, 覆盖到 since 即提前终止, 避免全量分页。
    返回结果仍保证整体升序, 边界裁剪由 provider._clip 精确完成。
    since 可为 date(上游从 Parquet Date 列取出), 归一化为当日末以含当日。
    """
    client = get_client()
    if since is None:
        result = _to_jsonable(client.bars.all(code, period=period))
        bars = list((result or {}).get("bars") or [])
        bars.sort(key=lambda b: b.get("time") or "")
        return bars
    since_naive = (
        since.replace(tzinfo=None)
        if isinstance(since, datetime)
        else datetime.combine(since, dtime.max)
    )
    out: list[dict] = []
    start = 0
    for _ in range(_MAX_BAR_PAGES):
        page = (
            _to_jsonable(client.bars.get(code, period=period, start=start, count=_BAR_PAGE_SIZE))
            or {}
        )
        bars = list(page.get("bars") or [])
        if not bars:
            break
        out.extend(bars)
        page_count = page.get("request_count") or len(bars)
        oldest = min((_parse_bar_time(b.get("time")) for b in bars), default=None)
        if oldest is not None and oldest <= since_naive:
            break
        if page_count < _BAR_PAGE_SIZE:
            break
        start += page_count
    out.sort(key=lambda b: b.get("time") or "")
    return out


def _parse_bar_time(value: str | None) -> datetime | None:
    """解析 bar 的 ISO 时间(含 +08:00)为 naive 北京时间, 解析失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def factors(code: str) -> list[dict]:
    """拉取标的逐日前复权/后复权因子, 返回按时间升序的 item dict 列表。

    item dict 含: time(ISO 含 +08:00) / qfq_factor / hfq_factor。
    qfq_factor 为前复权累积因子(最新≈1, 历史更小), 口径见 provider._factor_rows。
    """
    client = get_client()
    result = _to_jsonable(client.helpers.factors(code))
    items = list((result or {}).get("items") or [])
    items.sort(key=lambda i: i.get("time") or "")
    return items


def _as_day(trade_date: datetime | date | str) -> str:
    if isinstance(trade_date, datetime):
        return trade_date.date().isoformat()
    if isinstance(trade_date, date):
        return trade_date.isoformat()
    return str(trade_date)


def auction_data(code: str, trade_date: datetime | date | str | None = None) -> dict:
    """打包竞价过程 + 09:25 正式撮合。date=None 为当日过程。"""
    client = get_client()
    kwargs: dict = {"code": code}
    if trade_date is not None:
        kwargs["date"] = _as_day(trade_date)
    return _to_jsonable(client.helpers.auction_data(**kwargs)) or {}


def market_rank(
    *,
    category: str = "沪深A股",
    sort_by: str = "涨幅",
    count: int = 200,
    ascending: bool = False,
) -> list[dict]:
    """全市场分类实时排行 (0x054B), 用于竞价 Tier 1 初筛。

    返回 row dict 列表: full_code / name / change_pct(百分数) / amount(元) /
    volume_hand(手) / opening_rush(百分数) / seal_amount(元)。
    """
    client = get_client()
    table = client.helpers.realtime_rank(
        category=category, sort_by=sort_by, count=count, ascending=ascending
    )
    rows = getattr(table, "rows", None) or ()
    out: list[dict] = []
    for row in rows:
        out.append(
            {
                "full_code": str(getattr(row, "full_code", "") or ""),
                "name": getattr(row, "name", None),
                "change_pct": getattr(row, "change_pct", None),
                "amount": getattr(row, "amount", None),
                "volume_hand": getattr(row, "volume_hand", None),
                "opening_rush": getattr(row, "opening_rush", None),
                "seal_amount": getattr(row, "seal_amount", None),
            }
        )
    return out


def snapshots(codes: list[str]) -> list[dict]:
    """批量行情快照, 返回快照 dict 列表。

    快照 dict 含: code / exchange / last_price / pre_close_price / open_price /
    high_price / low_price / total_hand(手) / amount(元)。
    """
    client = get_client()
    return _to_jsonable(client.quotes.get_snapshots(codes)) or []


def a_share_codes() -> list[str]:
    """全 A 股代码列表(eltdx 格式 sh600000), 进程内缓存一天。"""
    global _a_share_codes_cache
    now = time.time()
    if _a_share_codes_cache and now - _a_share_codes_cache[0] < _CACHE_TTL:
        return list(_a_share_codes_cache[1])
    client = get_client()
    codes = list(client.codes.all_a_shares() or [])
    if not codes:
        raise EltdxBridgeError("eltdx 返回空 A 股代码表")
    _a_share_codes_cache = (now, codes)
    return list(codes)


def code_names() -> dict[str, str]:
    """全市场证券名称映射 {eltdx_code: name}, 进程内缓存一天。

    通过三市场代码表分页拉取(SecurityCode 含 name 字段)。
    """
    global _code_names_cache
    now = time.time()
    if _code_names_cache and now - _code_names_cache[0] < _CACHE_TTL:
        return dict(_code_names_cache[1])
    client = get_client()
    names: dict[str, str] = {}
    for market in _MARKETS:
        for page in range(_MAX_CODE_PAGES):
            rows = (
                _to_jsonable(
                    client.codes.list(
                        market=market, start=page * _CODE_PAGE_SIZE, limit=_CODE_PAGE_SIZE
                    )
                )
                or []
            )
            for row in rows:
                code = f"{row.get('exchange') or ''}{row.get('code') or ''}"
                if code:
                    names[code] = row.get("name") or code
            if len(rows) < _CODE_PAGE_SIZE:
                break
    if not names:
        raise EltdxBridgeError("eltdx 返回空代码表")
    _code_names_cache = (now, names)
    return dict(names)


def share_capitals() -> dict[str, dict]:
    """全市场流通/总股本与上市日映射 {eltdx_code: {float_shares, total_shares, ipo_date}}。

    通过 corporate.finance_batch 分批拉取财务快照。liu_tong_gu_ben_raw_float /
    zong_gu_ben_raw_float 单位是万股, 乘以 10000 转股, 对齐项目 canonical float_shares /
    total_shares 的"股"口径(见 pipeline 换手率公式 volume(手) * 10000 / float_shares(股))。
    ipo_date 保持 ISO 字符串(纯 Python 结构), 由 provider 转 date。
    进程内缓存一天。
    """
    global _share_capitals_cache
    now = time.time()
    if _share_capitals_cache and now - _share_capitals_cache[0] < _CACHE_TTL:
        return dict(_share_capitals_cache[1])
    client = get_client()
    codes = a_share_codes()
    caps: dict[str, dict] = {}
    for i in range(0, len(codes), _FINANCE_BATCH):
        chunk = codes[i : i + _FINANCE_BATCH]
        page = _to_jsonable(client.corporate.finance_batch(chunk)) or {}
        for row in (page.get("records") or []):
            code = f"{row.get('exchange') or ''}{row.get('code') or ''}"
            if not code:
                continue
            liu = row.get("liu_tong_gu_ben_raw_float")
            zong = row.get("zong_gu_ben_raw_float")
            caps[code] = {
                "float_shares": liu * 10000.0
                if isinstance(liu, (int, float)) and not isinstance(liu, bool)
                else None,
                "total_shares": zong * 10000.0
                if isinstance(zong, (int, float)) and not isinstance(zong, bool)
                else None,
                "ipo_date": row.get("ipo_date"),
            }
    if not caps:
        raise EltdxBridgeError("eltdx 返回空财务快照")
    _share_capitals_cache = (now, caps)
    return dict(caps)
