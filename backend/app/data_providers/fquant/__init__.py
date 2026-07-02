"""FQuantProvider v2 — 直连 fstore PG + engine-data + moneyflow 三上游源。

子模块（§4.1 文件结构）：
- ``symbols``          符号归一
- ``fstore_client``    fstore PostgreSQL 直连（psycopg v3，fallback psycopg2）
- ``engine_data_client`` engine-data HTTP 客户端
- ``moneyflow_client`` moneyflow HTTP 客户端
- ``mapping``          上游字段 → 内部 schema 转换
- ``adj_factor``       xdxr 事件 → 累积 ex_factor 计算
- ``fallback``         三源降级策略表

对外由 ``app.data_providers.fquant_provider.FQuantProvider`` 聚合。
"""
from __future__ import annotations

# 重导出符号归一工具（兼容 PoC 测试脚本的 from fquant_provider import split_symbol 等）
from app.data_providers.fquant.symbols import (
    code_and_market_to_symbol,
    code_to_symbol,
    exchange_of,
    is_a_stock,
    split_symbol,
    symbol_to_code,
    symbol_to_market,
)

__all__ = [
    "code_and_market_to_symbol",
    "code_to_symbol",
    "exchange_of",
    "is_a_stock",
    "split_symbol",
    "symbol_to_code",
    "symbol_to_market",
]
