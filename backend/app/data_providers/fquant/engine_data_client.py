"""engine-data API 客户端（§2.2 / §4.1）。

端点基址：``http://192.168.5.99:8099``（§6.1 ``FQUANT_ENGINE_DATA_BASE``）
路由：``GET /api/v1/{dataset}/{code}[?date=YYYYMMDD&limit=N]``
dataset 白名单：``{day, wide, minutes, trans, xdxr, chips}``（§2.2）

错误降级（§7.2）：
- L1：单 symbol 4xx/5xx → 跳过，warning
- L2：连接超时 / DNS → warning，调用方走备份源
- chips 端点实测超时，不允许接入（§1.2-N6 / §7.4）
"""
from __future__ import annotations

import logging
import os
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 配置（§6.1）
# --------------------------------------------------------------------------- #
ENGINE_DATA_BASE = os.getenv("FQUANT_ENGINE_DATA_BASE", "http://192.168.5.99:8099")
ENGINE_DATA_TIMEOUT = float(os.getenv("FQUANT_ENGINE_DATA_TIMEOUT", "8"))


class EngineDataClient:
    """engine-data API 客户端。

    端点：
    - ``GET /api/v1/day/{code}?limit=N`` → 日 K 线
    - ``GET /api/v1/wide/{code}?limit=N`` → daily 增强字段（§2.2）
    - ``GET /api/v1/minutes/{code}?date=YYYYMMDD`` → 分钟级 tick
    - ``GET /api/v1/trans/{code}?date=YYYYMMDD`` → 分笔成交
    - ``GET /api/v1/xdxr/{code}`` → 除权除息

    通用响应结构（§2.2）::

        {
          "dataset": "...", "code": "...", "cache_id": "...",
          "date": "YYYYMMDD",  # 仅 ?date= 时填充
          "count": <int>, "rows": [...],
          "source": "...", "truncated": true|false
        }
    """

    def __init__(self) -> None:
        self.base = ENGINE_DATA_BASE.rstrip("/")
        self.timeout = ENGINE_DATA_TIMEOUT

    # ------------------------------------------------------------------ #
    # 底层 GET
    # ------------------------------------------------------------------ #
    def get(self, path: str, **params) -> dict | None:
        """GET 请求，失败返回 None（L1/L2）。

        :param path: 相对路径（如 ``/api/v1/wide/600519``）
        :param params: 查询参数（如 ``limit=250``, ``date="20260701"``）
        :return: JSON dict 或 None
        """
        url = f"{self.base}{path}"
        try:
            resp = httpx.get(url, params=params, timeout=self.timeout, trust_env=False)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("EngineData GET %s 失败: %s", path, e)
            return None

    # ------------------------------------------------------------------ #
    # 各 dataset 封装
    # ------------------------------------------------------------------ #
    def get_wide(self, code: str, limit: int = 250) -> list[dict]:
        """``wide`` dataset — daily 增强字段（主源，§4.4）。

        每行含：date/datetime/open/high/low/close/volume/amount/last_close/
        change_rate/inner_*/outer_*/close_volume/open_volume 等（§2.2 / 附录 A.1）。
        """
        data = self.get(f"/api/v1/wide/{quote(code)}", limit=limit)
        return (data or {}).get("rows") or []

    def get_day(self, code: str, limit: int = 250) -> list[dict]:
        """``day`` dataset — 基础日 K（fallback for wide）。

        每行含：date/open/high/low/close/volume/amount（§2.2）。
        """
        data = self.get(f"/api/v1/day/{quote(code)}", limit=limit)
        return (data or {}).get("rows") or []

    def get_minutes(self, code: str, date_yyyymmdd: str, limit: int = 5000) -> list[dict]:
        """``minutes`` dataset — 分钟级 tick（§4.6）。

        每行含：price/volume。时间戳由客户端重建（mapping.py: ``generated_minute_time``）。
        :param date_yyyymmdd: 日期格式 ``YYYYMMDD``
        """
        data = self.get(f"/api/v1/minutes/{quote(code)}", date=date_yyyymmdd, limit=limit)
        return (data or {}).get("rows") or []

    def get_trans(self, code: str, date_yyyymmdd: str, limit: int = 5000) -> list[dict]:
        """``trans`` dataset — 分笔成交（§3.4 扩展方法 ``get_transactions``）。

        每行含：time/price/volume/amount/order_count/direction。
        direction 实测为数字：0=中性 / 1=买 / 2=卖（§2.2）。
        """
        data = self.get(f"/api/v1/trans/{quote(code)}", date=date_yyyymmdd, limit=limit)
        return (data or {}).get("rows") or []

    def get_xdxr(self, code: str, limit: int = 100) -> list[dict]:
        """``xdxr`` dataset — 除权除息（§4.5 主源）。

        每行含：date/fenhong/fenshu/songzhuangu/peigu/peigujia/category/qianzongguben/
        houzongguben/...（§2.2 / 附录 A.2）。
        - fenhong = 每 10 股派现
        - fenshu = 每 10 股送股
        - category: 1=除权除息 / 5=股本变化
        """
        data = self.get(f"/api/v1/xdxr/{quote(code)}", limit=limit)
        return (data or {}).get("rows") or []
