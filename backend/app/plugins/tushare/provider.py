"""Tushare 集合竞价数据源 provider。

方法签名对齐 custom.GenericHTTPProvider 的可发现约定: 注入 loader 注册表后,
auction hub 通过 provider_has_dataset("auction") 路由到本插件。

当前实现数据集: auction (仅 09:25 正式撮合的日级结果)。
不提供 09:15-09:25 过程序列 → get_auction_series 恒为空列表, 不伪造点。
未声明 daily / minute / realtime / financial → provider_has_dataset 为 False, 回退 tickflow。

单位口径 (CONTRIBUTING §3, auction contracts, 禁止启发式换算):
  - Tushare stk_auction.vol 文档为股 → 内部 open_volume 手 (/100)
  - Tushare stk_auction.amount 文档为元 → 内部 open_amount 元
  - Tushare stk_auction.price 为成交均价 → vwap, 不是开盘撮合价
  - Tushare stk_auction_o.close 为开盘集合竞价收盘价 → open_price
  - Tushare turnover_rate 为换手率(%) → 内部百分数值, 透传
  - open_change_pct 由 open_price/pre_close 推导为小数制
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from app.auction.contracts import AuctionFinal, cn_datetime, datetime_to_ms
from app.auction.sources import CAP_FINALS, as_float, shares_to_hands
from app.market_time import cn_today

logger = logging.getLogger(__name__)

_DATASETS = ("auction",)
API_KEY_ENV = "TUSHARE_TOKEN"
SECRETS_FIELD = "tushare_api_key"


def get_api_key() -> str:
    from app import secrets_store

    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def _import_tushare():
    import tushare as ts

    return ts


def availability() -> tuple[bool, str]:
    """loader 启动自检: SDK 已装且 Token 已配置才注册为可切换。不抛异常。"""
    try:
        _import_tushare()
    except ImportError:
        return False, "未安装 tushare, 运行: uv pip install tushare"
    if not get_api_key():
        return False, f"未配置 {API_KEY_ENV}(可在设置页数据源卡片中直接填写)"
    return True, "ok"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Token 实探一次 stock_basic(先探后存)。不落盘。"""
    try:
        ts = _import_tushare()
    except ImportError:
        return False, "未安装 tushare"
    try:
        pro = ts.pro_api(api_key)
        frame = pro.stock_basic(list_status="L", fields="ts_code", limit=1)
    except Exception as exc:
        return False, f"Token 无效或网络失败: {exc}"
    if frame is None or getattr(frame, "empty", True):
        return False, "Token 可用但 stock_basic 返回空"
    return True, "ok"


@dataclass
class _TushareConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "tushare"
    display_name: str = "Tushare"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class TushareProvider:
    name = "tushare"
    builtin = True
    auction_capabilities = (CAP_FINALS,)
    auction_finals_universe = True

    def __init__(self) -> None:
        self.config = _TushareConfig()
        self._pro = None

    def close(self) -> None:
        self._pro = None

    def _get_pro(self):
        if self._pro is not None:
            return self._pro
        try:
            ts = _import_tushare()
        except ImportError:
            return None
        token = get_api_key()
        if not token:
            return None
        self._pro = ts.pro_api(token)
        return self._pro

    def available(self) -> tuple[bool, str]:
        return availability()

    def get_auction_series(self, symbols: list[str], trade_date: date) -> list:
        del symbols, trade_date
        return []

    def get_auction_finals(
        self,
        symbols: list[str] | None,
        trade_date: date,
    ) -> list[AuctionFinal]:
        return self._load_open_finals(trade_date, symbols)

    def _load_open_finals(
        self,
        trade_date: date,
        symbols: list[str] | None,
    ) -> list[AuctionFinal]:
        same_day = self._call_finals(
            "stk_auction",
            trade_date,
            symbols,
            price_field="",
            vwap_field="price",
            extra_flags=["tushare_stk_auction_vwap"],
        )
        daily = self._call_finals(
            "stk_auction_o",
            trade_date,
            symbols,
            price_field="close",
            vwap_field="vwap",
        )
        by_symbol: dict[str, AuctionFinal] = {item.symbol: item for item in same_day}
        for item in daily:
            current = by_symbol.get(item.symbol)
            if current is None:
                by_symbol[item.symbol] = item
                continue
            # stk_auction.price 是均价, stk_auction_o.close 才是撮合价
            current.open_price = item.open_price
            current.vwap = current.vwap or item.vwap
            current.pre_close = current.pre_close or item.pre_close
            current.open_volume = current.open_volume if current.open_volume is not None else item.open_volume
            current.open_amount = current.open_amount if current.open_amount is not None else item.open_amount
            current.turnover_rate = current.turnover_rate if current.turnover_rate is not None else item.turnover_rate
            current.volume_ratio = current.volume_ratio if current.volume_ratio is not None else item.volume_ratio
            if item.open_price is not None and current.pre_close not in (None, 0):
                current.open_change_pct = item.open_price / current.pre_close - 1.0
            if "vwap_open_price_distinct" not in current.quality_flags:
                current.quality_flags.append("vwap_open_price_distinct")
        return list(by_symbol.values())

    def _call_finals(
        self,
        api_name: str,
        trade_date: date,
        symbols: list[str] | None,
        *,
        price_field: str,
        vwap_field: str,
        extra_flags: list[str] | None = None,
    ) -> list[AuctionFinal]:
        pro = self._get_pro()
        if pro is None:
            return []
        method = getattr(pro, api_name, None)
        if method is None:
            logger.warning("tushare 无接口 %s", api_name)
            return []
        try:
            frame = method(trade_date=trade_date.strftime("%Y%m%d"))
        except Exception as exc:
            logger.warning("tushare %s failed: %s", api_name, exc)
            return []
        if frame is None or getattr(frame, "empty", True):
            return []
        wanted = {s.upper() for s in symbols} if symbols else None
        available_at = datetime_to_ms(cn_datetime(trade_date, 9, 25, 30))
        out: list[AuctionFinal] = []
        for raw in frame.to_dict("records"):
            symbol = str(raw.get("ts_code") or "").upper()
            if not symbol:
                continue
            if wanted is not None and symbol not in wanted:
                continue
            open_price = as_float(raw.get(price_field)) if price_field else None
            pre_close = as_float(raw.get("pre_close") or raw.get("pre_close_price"))
            change = None
            if open_price is not None and pre_close not in (None, 0):
                change = open_price / pre_close - 1.0
            flags = list(extra_flags or [])
            flags.append(f"tushare_{api_name}")
            out.append(
                AuctionFinal(
                    trade_date=trade_date,
                    symbol=symbol,
                    source="tushare",
                    available_at_ms=available_at,
                    open_price=open_price,
                    vwap=as_float(raw.get(vwap_field)) if vwap_field else None,
                    open_volume=shares_to_hands(raw.get("vol")),
                    open_amount=as_float(raw.get("amount")),
                    pre_close=pre_close,
                    turnover_rate=as_float(raw.get("turnover_rate")),
                    volume_ratio=as_float(raw.get("volume_ratio")),
                    open_change_pct=change,
                    quality_flags=flags,
                )
            )
        return out

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset != "auction":
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "error": f"tushare 插件未接入 {dataset} 数据集(自动回退 TickFlow)",
            }
        try:
            rows = self.get_auction_finals(symbols, cn_today())
        except Exception as exc:
            return {"provider": self.name, "dataset": "auction", "rows": 0, "error": str(exc)}
        preview = [item.to_row() for item in rows[:5]]
        for item in preview:
            item["trade_date"] = str(item.get("trade_date") or "")
        return {
            "provider": self.name,
            "dataset": "auction",
            "rows": len(rows),
            "columns": list(preview[0].keys()) if preview else [],
            "preview": preview,
        }
