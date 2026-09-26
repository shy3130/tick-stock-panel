"""AKShare 接口薄封装。

隔离 pandas: akshare 各接口返回 pandas.DataFrame, 本层统一转 polars 后交给
provider, provider 与测试不接触 pandas/akshare 类型。akshare 本身体积大,
import 放在构造时(loader 的 availability 自检只查 spec, 不触发本模块 import)。
"""

from __future__ import annotations

import polars as pl


class AkshareError(Exception):
    """akshare 调用失败的统一封装(网络/限频/字段变更等原始异常信息保留)。"""


class AkshareClient:
    def __init__(self) -> None:
        import akshare as ak

        self._ak = ak

    def _call(self, fn, **kwargs) -> pl.DataFrame:
        try:
            df = fn(**kwargs)
        except Exception as e:
            raise AkshareError(str(e)) from e
        if df is None or len(df) == 0:
            return pl.DataFrame()
        return pl.from_pandas(df)

    # 东财全市场批量报表: 按报告期(yyyymmdd)一次请求覆盖全部 A 股
    def income_market(self, period: str) -> pl.DataFrame:
        return self._call(self._ak.stock_lrb_em, date=period)

    def balance_sheet_market(self, period: str) -> pl.DataFrame:
        return self._call(self._ak.stock_zcfz_em, date=period)

    def cash_flow_market(self, period: str) -> pl.DataFrame:
        return self._call(self._ak.stock_xjll_em, date=period)

    def metrics_market(self, period: str) -> pl.DataFrame:
        return self._call(self._ak.stock_yjbb_em, date=period)

    # 股本结构为单标的接口 (symbol 形如 "600519.SH")
    def share_structure(self, symbol: str) -> pl.DataFrame:
        return self._call(self._ak.stock_zh_a_gbjg_em, symbol=symbol)

    def close(self) -> None:  # 与 FuyaoProvider 契约对齐; akshare 无显式会话
        return None
