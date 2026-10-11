"""AKShare(开源财经数据接口库)内置财务数据源 provider。

方法签名对齐 custom.GenericHTTPProvider (service 分流点按这套签名调用),
注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

实现数据集:
  - financial    财务五表。三大报表/指标走东财全市场批量接口(stock_lrb_em /
                 stock_zcfz_em / stock_xjll_em / stock_yjbb_em), 按报告期一次
                 请求覆盖全市场再筛目标标的, 避免逐股请求风暴; shares 走
                 stock_zh_a_gbjg_em 单标的接口(带节流)。
                 未声明 realtime/daily 等 → provider_has_dataset 为 False,
                 自动回退 tickflow。

单位与口径 (CONTRIBUTING §3.1, 不可凭字段名推断):
  - 批量利润表/业绩报表的「净利润」字段实测为归母口径 (东财 PARENT_NETPROFIT,
    以贵州茅台 2025 中报 454.0 亿对拍确认), 映射 net_income_attributable,
    不映射 net_income (含少数股东损益的总净利批量接口不提供)。
  - 业绩报表 roe/gross_margin/yoy 均为百分数数值 (17.89 = 17.89%),
    与项目 metrics 因子口径一致, 直接透传。
  - 「公告日期」为上海零点 epoch ms (与扶摇 *ms 同口径, +8h 后按 UTC 取日期);
    股本「变更日期」同为 ms, 作为 shares.period_end (股本生效日)。
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import polars as pl

logger = logging.getLogger(__name__)

_DATASETS = ("financial",)

_SH_MS = 28_800_000  # 东财 *ms 为北京时间零点 (= UTC 前一日 16:00), +8h 按 UTC 解析
_FINANCIAL_HISTORY_PERIODS = 8  # 财务首装全量历史: 最近 8 期季报(约 2 年), 对齐扶摇口径
_LATEST_PERIODS = 2  # latest_only 拉最近 2 个候选报告期按标的取最新, 覆盖披露季错峰
_SHARE_INTERVAL_S = 0.2  # 股本结构单标的请求节流 (公开接口, 频率保守)

# 表名 → 批量接口 method; shares 为单标的接口, 单独分支
_BULK_TABLES = {
    "income": "income_market",
    "balance_sheet": "balance_sheet_market",
    "cash_flow": "cash_flow_market",
    "metrics": "metrics_market",
}

# 东财批量报表原始列 → 项目 canonical 列名 (TickFlow 口径, 前端财务页与回测
# FUNDAMENTAL_FACTORS 按此消费)。映射之外的列不透传: 中文列名混入 canonical
# schema 会污染下游聚合, 且未核口径的字段按红线不猜不造。
_INCOME_FIELD_MAP = {
    "营业总收入": "revenue",
    "营业总支出-营业支出": "operating_cost",
    "营业总支出-销售费用": "selling_expense",
    "营业总支出-管理费用": "admin_expense",
    "营业总支出-财务费用": "financial_expense",
    "营业利润": "operating_profit",
    "利润总额": "total_profit",
    "净利润": "net_income_attributable",  # 实测归母口径, 见模块 docstring
}
_BALANCE_FIELD_MAP = {
    "资产-货币资金": "cash_and_equivalents",
    "资产-应收账款": "accounts_receivable",
    "资产-总资产": "total_assets",
    "负债-总负债": "total_liabilities",
    "股东权益合计": "total_equity",
}
_CASHFLOW_FIELD_MAP = {
    "经营性现金流-现金流量净额": "net_operating_cash_flow",
    "投资性现金流-现金流量净额": "net_investing_cash_flow",
    "融资性现金流-现金流量净额": "net_financing_cash_flow",
    "净现金流-净现金流": "net_cash_change",
}
_METRICS_FIELD_MAP = {
    "每股收益": "eps_basic",
    "每股净资产": "bps",
    "净资产收益率": "roe",
    "销售毛利率": "gross_margin",
    "营业总收入-营业总收入": "revenue",
    "营业总收入-同比增长": "revenue_yoy",
    "净利润-净利润": "net_income_attributable",
    "净利润-同比增长": "net_income_yoy",
}
_TABLE_FIELD_MAPS = {
    "income": _INCOME_FIELD_MAP,
    "balance_sheet": _BALANCE_FIELD_MAP,
    "cash_flow": _CASHFLOW_FIELD_MAP,
    "metrics": _METRICS_FIELD_MAP,
}
_ANNOUNCE_COL = {
    "income": "公告日期",
    "balance_sheet": "公告日期",
    "cash_flow": "公告日期",
    "metrics": "最新公告日期",
}

_SHARE_FIELD_MAP = {
    "总股本": "total_shares",
    "已上市流通A股": "float_shares",  # A 股流通盘, 与换手率口径一致
    "流通受限股份": "restricted_shares",
}


def availability() -> tuple[bool, str]:
    """loader 启动自检: akshare 已装入后端环境才注册为可切换数据源。不抛异常。"""
    if importlib.util.find_spec("akshare") is not None:
        return True, "ok"
    return False, "缺少依赖 akshare(点击下方安装,或手动 pip install akshare)"


@dataclass
class _AkshareConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "akshare"
    display_name: str = "akshare"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_of_ms(value) -> str | None:
    """东财 *ms(上海零点) → ISO 日期。None/非法值返回 None, 不伪造。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        return value[:10]  # 已是 ISO 字符串
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp((ms + _SH_MS) // 1000, tz=UTC).date().isoformat()


def _bare_code(symbol: str) -> str:
    """'600519.SH' → '600519'; 已是裸代码原样返回。"""
    return symbol.split(".")[0].strip()


def _full_symbol(code: str) -> str:
    """东财裸 6 位代码 → 项目 canonical 后缀 (沪 .SH / 深 .SZ / 北 .BJ)。"""
    if code.startswith(("4", "8", "92")):
        return f"{code}.BJ"
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _recent_periods(today: date, n: int) -> list[str]:
    """≤ today 的最近 n 个季末报告期, 降序, yyyymmdd 格式。"""
    ends = []
    for yy in range(today.year - 3, today.year + 1):
        for mm, dd in ((3, 31), (6, 30), (9, 30), (12, 31)):
            d = date(yy, mm, dd)
            if d <= today:
                ends.append(d)
    return [d.strftime("%Y%m%d") for d in sorted(ends, reverse=True)[:n]]


class AkshareProvider:
    """AKShare 财务数据源。"""

    name = "akshare"
    builtin = True

    def __init__(self) -> None:
        self.config = _AkshareConfig()
        self._client = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self):
        if self._client is None:
            from app.plugins.akshare.client import AkshareClient

            self._client = AkshareClient()
        return self._client

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """拉取财务数据, 映射为 canonical 列(symbol/period_end/announce_date/指标)。

        - 三大报表/metrics: 全市场批量接口按报告期一次拉取再筛标的;
          latest_only 拉最近 2 个候选期后按标的取最新期(披露季错峰时
          避免已披露新期的标的挤掉未披露标的的旧期数据)。
        - shares: 单标的股本结构接口, 带节流; latest_only 取最新一条变更记录。
        """
        if table == "shares":
            return self._shares(symbols, latest_only)
        if table in _BULK_TABLES:
            return self._bulk_table(table, symbols, latest_only)
        return pl.DataFrame()

    def _bulk_table(self, table: str, symbols: list[str], latest_only: bool) -> pl.DataFrame:
        client = self._get_client()
        method = _BULK_TABLES[table]
        field_map = _TABLE_FIELD_MAPS[table]
        codes = {_bare_code(s) for s in symbols if s}
        if not codes:
            return pl.DataFrame()

        n = _LATEST_PERIODS if latest_only else _FINANCIAL_HISTORY_PERIODS
        rows_out: list[dict] = []
        for period in _recent_periods(date.today(), n):
            try:
                df = getattr(client, method)(period)
            except Exception as e:
                logger.warning("akshare %s %s 期拉取失败: %s", table, period, e)
                continue
            if df.is_empty() or "股票代码" not in df.columns:
                continue
            period_iso = f"{period[:4]}-{period[4:6]}-{period[6:]}"
            for row in df.filter(pl.col("股票代码").cast(pl.Utf8).is_in(sorted(codes))).iter_rows(
                named=True
            ):
                code = str(row.get("股票代码") or "")
                out: dict = {
                    "symbol": _full_symbol(code),
                    "period_end": period_iso,
                    "announce_date": _iso_of_ms(row.get(_ANNOUNCE_COL[table])),
                }
                for src, dst in field_map.items():
                    out[dst] = _to_float(row.get(src))
                rows_out.append(out)

        if not rows_out:
            return pl.DataFrame()
        result = pl.DataFrame(rows_out, infer_schema_length=None)
        if latest_only:
            # 每股保留最新报告期一行 (批量接口行序不保证, 显式按 period_end 取 max)
            result = result.sort("period_end").group_by("symbol", maintain_order=True).last()
        return result

    def _shares(self, symbols: list[str], latest_only: bool) -> pl.DataFrame:
        client = self._get_client()
        rows_out: list[dict] = []
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_SHARE_INTERVAL_S)
            try:
                df = client.share_structure(sym)
            except Exception as e:
                logger.warning("akshare 股本结构 %s 失败: %s", sym, e)
                continue
            if df.is_empty() or "变更日期" not in df.columns:
                continue
            for row in df.sort("变更日期", descending=True).iter_rows(named=True):
                period_end = _iso_of_ms(row.get("变更日期"))
                if period_end is None:
                    continue
                out = {"symbol": sym, "period_end": period_end}
                for src, dst in _SHARE_FIELD_MAP.items():
                    out[dst] = _to_float(row.get(src))
                rows_out.append(out)
                if latest_only:
                    break
        return pl.DataFrame(rows_out, infer_schema_length=None) if rows_out else pl.DataFrame()
