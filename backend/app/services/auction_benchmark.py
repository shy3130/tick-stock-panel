"""短线风向标服务 (fuyao 专有) — 复盘页卡片 + AI 复盘上下文。

非路由数据集: tickflow 无对应能力, 直接经 custom_sources 调 fuyao provider;
fuyao 未配置时返回 source_unavailable 状态, 前端降级提示。

数据契约 (实测 2026-08-30, 60 交易日回测 353 样本):
- 每日 5~6 只, 服务端筛选的竞价异动股, 附概念标签
- 名单当日 (开盘买→收盘卖) 均值 +0.54% vs 全市场 +0.10%, 有真实当日策略能力;
  但高开≥5% 子集当日 -1.97% (追高陷阱) → 前端对高开子集标「追高风险」
- 次日无显著优势 (+0.08%), 定位为「当日观察名单」而非隔夜轮动信号

缓存策略 (与 dragon_tiger 同模式):
- 历史名单不可变 → 按日落 JSON 缓存 (data/auction_benchmark/date=YYYY-MM-DD.json),
  缓存命中不触发插件注册表加载
- 收益 enrich (当日oc/全天/次日) 不落缓存 — 次日数据晚到, 读取时现算
- 当日名单不缓存 (竞价阶段名单可能变动, 以现拉为准)
- 显式日期失败 → 回退上一交易日一次 (state=fallback_prev)

日期解析: 接口对显式非交易日报 code=1002, 本层用本地 kline_daily 分区日期
把目标日回退到「≤目标日的最近交易日」, 规避报错。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import date as date_cls
from pathlib import Path

import polars as pl

from app.market_time import cn_today

logger = logging.getLogger(__name__)

_DATE_DIR_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")


def _local_trading_days(data_dir: Path) -> list[date_cls]:
    """本地日K分区日期 = 已知交易日集合 (升序)。扫描失败返回空。"""
    root = data_dir / "kline_daily"
    out: list[date_cls] = []
    try:
        for d in root.iterdir():
            m = _DATE_DIR_RE.match(d.name)
            if d.is_dir() and m:
                try:
                    out.append(date_cls.fromisoformat(m.group(1)))
                except ValueError:
                    continue
    except OSError:
        return []
    return sorted(out)


def resolve_trade_date(data_dir: Path, target: date_cls | None) -> date_cls | None:
    """目标日 → ≤目标日的最近本地交易日。None → None (由 fuyao 取当日)。"""
    if target is None:
        return None
    days = _local_trading_days(data_dir)
    if not days:
        return target
    candidates = [d for d in days if d <= target]
    return max(candidates) if candidates else target


def _prev_trading_day(data_dir: Path, d: date_cls) -> date_cls | None:
    days = _local_trading_days(data_dir)
    earlier = [x for x in days if x < d]
    return max(earlier) if earlier else None


def _next_trading_day(data_dir: Path, d: date_cls) -> date_cls | None:
    days = _local_trading_days(data_dir)
    later = [x for x in days if x > d]
    return min(later) if later else None


def _provider():
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider("fuyao"):
        return None
    return custom_sources.get_provider("fuyao")


def _cache_path(data_dir: Path, d: date_cls) -> Path:
    return data_dir / "auction_benchmark" / f"date={d.isoformat()}.json"


def _load_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _store_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _raw_items(data: dict) -> list[dict]:
    items = data.get("item")
    return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []


def _read_kline_closes(data_dir: Path, d: date_cls) -> dict[str, float]:
    """某交易日全市场 {symbol: close}; 无分区/读失败返回空。"""
    root = data_dir / "kline_daily" / f"date={d.isoformat()}"
    try:
        files = sorted(root.glob("*.parquet"))
        if not files:
            return {}
        df = pl.concat([pl.read_parquet(f, columns=["symbol", "close"]) for f in files])
        return dict(zip(df["symbol"].to_list(), df["close"].to_list()))
    except (OSError, pl.exceptions.PolarsError):
        return {}


def _enrich(data_dir: Path, trade_date: date_cls, items: list[dict]) -> list[dict]:
    """名单叠加真实收益: day0_oc (开盘买→收盘卖) / day0_pct (全天) / d1_pct (次日)。

    用相邻 kline_daily 分区现算, 不落缓存 — 次日分区晚到时先给 None, 到了自然补上。
    """
    root = data_dir / "kline_daily" / f"date={trade_date.isoformat()}"
    day0: dict[str, tuple[float, float]] = {}  # symbol -> (open, close)
    try:
        files = sorted(root.glob("*.parquet"))
        if files:
            df = pl.concat([pl.read_parquet(f, columns=["symbol", "open", "close"]) for f in files])
            day0 = dict(zip(df["symbol"].to_list(), zip(df["open"].to_list(), df["close"].to_list())))
    except (OSError, pl.exceptions.PolarsError):
        day0 = {}

    prev = _prev_trading_day(data_dir, trade_date)
    prev_close = _read_kline_closes(data_dir, prev) if prev else {}
    nxt = _next_trading_day(data_dir, trade_date)
    next_close = _read_kline_closes(data_dir, nxt) if nxt else {}

    out: list[dict] = []
    for r in items:
        sym = str(r.get("thscode") or "")
        oc = pct = d1 = None
        if sym in day0:
            o, c = day0[sym]
            if o and o > 0 and c is not None:
                oc = c / o - 1.0
            pc = prev_close.get(sym)
            if pc and pc > 0 and c is not None:
                pct = c / pc - 1.0
            nc = next_close.get(sym)
            if c and c > 0 and nc is not None:
                d1 = nc / c - 1.0
        out.append({
            "thscode": sym,
            "ticker": r.get("ticker"),
            "name": r.get("name"),
            "auction_pct": r.get("auction_pct"),
            "tags": [str(t) for t in (r.get("tags") or [])],
            "day0_oc": oc,
            "day0_pct": pct,
            "d1_pct": d1,
        })
    return out


def _base_payload(data_dir: Path, trade_date: date_cls, data: dict) -> dict:
    return {
        "state": "ok",
        "requested_date": trade_date.isoformat(),
        "trade_date": str(data.get("date") or trade_date.isoformat()),
        "count": len(_raw_items(data)),
        "raw_items": _raw_items(data),
    }


def get_auction_benchmark(data_dir: Path, target: date_cls | None = None) -> dict:
    """取短线风向标名单 (含当日/次日真实收益)。返回给前端的统一容器。

    state: ok (正常) | fallback_prev (目标日拉取失败, 已回退上一期)
          | source_unavailable (未配置 fuyao) | no_data (拉取失败)
    """
    days = _local_trading_days(data_dir)
    if target is None:
        target = max(days) if days else None
    trade_date = resolve_trade_date(data_dir, target)
    today = cn_today()

    # 历史日缓存优先 (纯本地, 不触发插件注册表加载)
    if trade_date is not None and trade_date < today:
        cached = _load_cache(_cache_path(data_dir, trade_date))
        if cached is not None:
            return _respond(data_dir, trade_date, cached, cached.get("state") or "ok")

    provider = _provider()
    if provider is None:
        return {"state": "source_unavailable"}

    from app.plugins.fuyao.client import FuyaoError

    explicit = trade_date.isoformat() if trade_date is not None else None
    try:
        data = provider.short_term_benchmark(explicit)
        try:
            actual = date_cls.fromisoformat(str(data.get("date")))
        except ValueError:
            actual = trade_date
        base = _base_payload(data_dir, actual, data)
        # 历史日不可变 → 落缓存; 当日不缓存 (竞价阶段名单可能变动)
        if actual is not None and actual < today:
            with contextlib.suppress(OSError):
                _store_cache(_cache_path(data_dir, actual), base)
        return _respond(data_dir, actual, base, "ok")
    except FuyaoError as e:
        # 显式日期失败 (非交易日/边界日) → 回退上一交易日一次
        if trade_date is not None:
            prev = _prev_trading_day(data_dir, trade_date)
            if prev is not None:
                try:
                    cached_prev = _load_cache(_cache_path(data_dir, prev))
                    if cached_prev is not None:
                        return _respond(data_dir, prev, cached_prev, "fallback_prev",
                                        requested=explicit)
                    data_prev = provider.short_term_benchmark(prev.isoformat())
                    base = _base_payload(data_dir, prev, data_prev)
                    with contextlib.suppress(OSError):
                        _store_cache(_cache_path(data_dir, prev), base)
                    return _respond(data_dir, prev, base, "fallback_prev",
                                    requested=explicit)
                except FuyaoError:
                    pass
        logger.warning("短线风向标拉取失败: %s", e)
        return {"state": "no_data", "message": str(e)}


def _respond(
    data_dir: Path,
    trade_date: date_cls | None,
    base: dict,
    state: str,
    requested: str | None = None,
) -> dict:
    """缓存/现拉的原始容器 → 叠加收益 enrich 后的前端容器。"""
    if trade_date is None:
        return {**base, "state": state}
    payload = {
        "state": state,
        "requested_date": requested if requested is not None else base.get("requested_date"),
        "trade_date": base.get("trade_date") or trade_date.isoformat(),
        "count": base.get("count") or 0,
        "items": _enrich(data_dir, trade_date, base.get("raw_items") or []),
    }
    return payload


def build_recap_context(data_dir: Path) -> str:
    """AI 复盘的盘前风向标摘要段 (纯文本, 失败返回空串不影响复盘)。"""
    try:
        payload = get_auction_benchmark(data_dir, None)
        if payload.get("state") not in ("ok", "fallback_prev"):
            return ""
        items = payload.get("items") or []
        if not items:
            return ""
        trade_date = payload.get("trade_date") or ""
        lines = [f"(数据日期: {trade_date})"]
        segs = []
        for i in items:
            seg = (f"{i.get('name')}({i.get('thscode')}) 竞价{(i.get('auction_pct') or 0):+.2f}%"
                   f"[{'·'.join(i.get('tags') or [])}]")
            if i.get("day0_oc") is not None:
                seg += f" → 当日开盘买{i['day0_oc']*100:+.2f}%"
            if i.get("d1_pct") is not None:
                seg += f", 次日{i['d1_pct']*100:+.2f}%"
            segs.append(seg)
        lines.append("盘前风向标名单: " + "; ".join(segs))
        ocs = [i["day0_oc"] for i in items if i.get("day0_oc") is not None]
        if ocs:
            lines.append(f"名单当日(开盘买→收盘卖)均值 {sum(ocs)/len(ocs)*100:+.2f}%")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — 摘要失败不影响复盘主流程
        logger.debug("盘前风向标复盘摘要构建失败: %s", e)
        return ""
