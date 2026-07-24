"""stock-sdk 内置数据源插件(免费行情, tickflow 的平替)。"""
from app.plugins.stocksdk.bridge import StockSDKBridgeError, availability, run_job
from app.plugins.stocksdk.provider import StockSDKProvider

PROVIDER_NAME = "stocksdk"

__all__ = [
    "PROVIDER_NAME",
    "StockSDKBridgeError",
    "StockSDKProvider",
    "availability",
    "run_job",
]
