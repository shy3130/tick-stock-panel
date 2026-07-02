"""本地源降级策略表（§7.1）。

本模块不执行降级逻辑，只提供降级链描述（用于日志/调试/文档）。
实际降级执行在 ``fquant_provider.py`` 各方法内实现。

降级矩阵（§7.1）：

| 方法/扩展            | 主源                     | 备份 1                    | 备份 2                 | 最坏兜底 |
|---------------------|--------------------------|--------------------------|------------------------|----------|
| get_instruments     | fstore base_infos        | —                        | —                      | 空 df    |
| get_daily           | engine-data wide         | fstore day_klines        | —                      | 空 df    |
| get_adj_factors     | engine-data xdxr         | fstore chuquan_chuxi     | —                      | 空 df    |
| get_minute          | engine-data minutes      | —                        | —                      | 空 df    |
| get_realtime        | tdx-api `/api/quote`      | fstore `daily_markets`   | —                      | 空 df    |
| get_financial       | fstore *_report_*        | —                        | —                      | 空 df    |
| get_moneyflow_daily | moneyflow /daily/stocks  | —                        | —                      | 空 df    |
| get_moneyflow_minute| moneyflow /minute/stocks | —                        | —                      | 空 df    |
| get_transactions    | engine-data trans        | —                        | —                      | 空 df    |
| get_corp_action     | fstore chuquan_chuxi     | engine-data xdxr         | —                      | 空 df    |

各级失败行为（§7.2）：
- L0：正常 → 直接归一返回
- L1：单 symbol 缺数 → 跳过，warning，继续
- L2：源连接失败 → warning，切换备份源，重试最多 1 次
- L3：envelope 异常（moneyflow code≠0）→ warning，跳过该方法所有 symbols
- L4：所有源失败 → 空 df，error 日志
- L5：致命（启动时 DB password 缺失 / import 失败）→ capabilities 降级，不抛
"""
from __future__ import annotations

from typing import Literal

# 降级级别
FailLevel = Literal["L0", "L1", "L2", "L3", "L4", "L5"]

# §7.1 降级矩阵（方法 → 备份源链）
FALLBACK_CHAIN: dict[str, list[str]] = {
    "get_instruments":      ["fstore:base_infos"],
    "get_daily":            ["engine-data:wide", "fstore:day_klines"],
    "get_adj_factors":      ["engine-data:xdxr", "fstore:chuquan_chuxi"],
    "get_minute":           ["engine-data:minutes"],
    "get_realtime":         ["tdx-api:quote", "fstore:daily_markets"],
    "get_financial":        ["fstore:financial_report_*"],
    "get_moneyflow_daily":  ["moneyflow:daily"],
    "get_moneyflow_minute": ["moneyflow:minute"],
    "get_transactions":     ["engine-data:trans"],
    "get_corp_action":      ["fstore:chuquan_chuxi", "engine-data:xdxr"],
}


def get_fallback_chain(method: str) -> list[str]:
    """返回某方法的降级备份源链（主源在前）。

    >>> get_fallback_chain("get_daily")
    ['engine-data:wide', 'fstore:day_klines']
    """
    return FALLBACK_CHAIN.get(method, [])
