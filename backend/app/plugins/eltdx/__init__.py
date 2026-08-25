"""eltdx 内置数据源插件(通达信 7709 协议行情)。"""

from app.plugins.eltdx.bridge import EltdxBridgeError, availability
from app.plugins.eltdx.provider import EltdxProvider

PROVIDER_NAME = "eltdx"

__all__ = [
    "PROVIDER_NAME",
    "EltdxBridgeError",
    "EltdxProvider",
    "availability",
]
