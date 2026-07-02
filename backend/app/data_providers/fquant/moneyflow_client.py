"""moneyflow API 客户端（§2.3 / §4.1）。

端点基址：``http://pve.wf:8090``（§6.1 ``FQUANT_MONEYFLOW_BASE``）
路由：
- ``GET /api/v1/moneyflow/daily/stocks?codes=...&date=YYYY-MM-DD``
- ``GET /api/v1/moneyflow/minute/stocks?codes=...&date=YYYY-MM-DD``

响应字段差异（§2.3 实测）：
- minute 单点字段 PascalCase（``Code/Name/TradeDate/BucketTime/TotalAmount/...``）
- daily 字段首字母小写（``code/source/total.{main_inflow,...}``）

错误降级（§7.2）：
- L3：``code != 0`` → 跳过该方法所有 symbols
- L2：连接超时 → 调用方走备份源

实测假阳性（§7.4）：
- minute 09:25 桶 ``NetAmount=0`` 是集合竞价假阳性，上层需自行跳过
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 配置（§6.1）
# --------------------------------------------------------------------------- #
MONEYFLOW_BASE = os.getenv("FQUANT_MONEYFLOW_BASE") or os.getenv(
    "FQUANT_MONEYFLOW_API_BASE", "http://pve.wf:8090",
)
MONEYFLOW_TIMEOUT = float(os.getenv("FQUANT_MONEYFLOW_TIMEOUT", "10"))


class MoneyflowClient:
    """moneyflow API 客户端。

    健康检查：``GET /api/v1/health → {"code":0,"message":"ok at ..."}``

    daily 响应（实测，§2.3 / 附录 A.3）::

        {"code":0,"data":[{"code":"600519","source":"tdx",
          "total":{"main_inflow":...,"main_outflow":...,"main_net":...,
                   "total_inflow":...,"total_outflow":...,"total_net":...,
                   "volume":...,"amount":...}}]}

    minute 响应（实测，§2.3）::

        {"code":0,"data":[{"code":"600519","records":[
          {"Code":"600519","Name":"","TradeDate":"2026-07-01",
           "BucketTime":"09:25","TotalAmount":...,"TotalVolume":...,
           "NetAmount":0,  # 09:25 集合竞价假阳性（§7.4）
           "SuperLargeInflow":...,"SuperLargeOutflow":...,"SuperLargeNet":...,
           "LargeInflow":...,"LargeOutflow":...,"LargeNet":...,
           "MediumInflow":...,"MediumOutflow":...,"MediumNet":...,
           "SmallInflow":...,"SmallOutflow":...,"SmallNet":...,
           "MainTraditionalInflow":...,"MainTraditionalOutflow":...,"MainTraditionalNet":...,
           "MainBroadInflow":...,"MainBroadOutflow":...,"MainBroadNet":...,
           "RetailInflow":...,"RetailOutflow":...,"RetailNet":...,
           "NeutralAmount":...,"UnknownAmount":...,
           "ValidCount":...,"InvalidCount":...,"Source":"tdx"}
        ],"source":"tdx"}]}
    """

    def __init__(self) -> None:
        self.base = MONEYFLOW_BASE.rstrip("/")
        self.timeout = MONEYFLOW_TIMEOUT

    # ------------------------------------------------------------------ #
    # 底层 GET
    # ------------------------------------------------------------------ #
    def _get(self, path: str, **params) -> dict | None:
        """GET 请求，失败返回 None（L1/L2）。"""
        url = f"{self.base}{path}"
        try:
            resp = httpx.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("Moneyflow GET %s 失败: %s", path, e)
            return None

    # ------------------------------------------------------------------ #
    # daily / minute
    # ------------------------------------------------------------------ #
    def get_daily(self, codes: list[str], date_iso: str) -> dict[str, dict]:
        """日级资金流。

        :param codes: 纯数字 code 列表（如 ``["600519"]``）
        :param date_iso: ISO 日期 ``YYYY-MM-DD``
        :return: ``{code: total_dict}``；失败返回空 dict（L3）
        """
        if not codes:
            return {}
        resp = self._get(
            "/api/v1/moneyflow/daily/stocks",
            codes=",".join(codes),
            date=date_iso,
        )
        if not resp or resp.get("code") != 0:
            if resp:
                logger.warning("Moneyflow daily envelope code=%s, message=%s",
                               resp.get("code"), resp.get("message"))
            return {}
        out: dict[str, dict] = {}
        for item in resp.get("data") or []:
            code = item.get("code")
            if code:
                out[str(code)] = item.get("total") or {}
        return out

    def get_minute(self, code: str, date_iso: str) -> list[dict]:
        """分钟级资金流。

        :param code: 纯数字 code（如 ``"600519"``）
        :param date_iso: ISO 日期 ``YYYY-MM-DD``
        :return: records 列表（PascalCase 字段）；失败返回空列表（L3）
        """
        resp = self._get(
            "/api/v1/moneyflow/minute/stocks",
            codes=code,
            date=date_iso,
        )
        if not resp or resp.get("code") != 0:
            if resp:
                logger.warning("Moneyflow minute envelope code=%s, message=%s",
                               resp.get("code"), resp.get("message"))
            return []
        data = resp.get("data") or []
        if not data:
            return []
        # data[0] 即目标 code 的记录集（实测一次请求单 code）
        return (data[0].get("records")) or []
