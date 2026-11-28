"""Fuyao HTTP envelope and raw history calls used by both data hosts."""

from __future__ import annotations

from decimal import Decimal

import httpx

BASE_URL = "https://fuyao.aicubes.cn"
HISTORICAL_BATCH_LIMIT = 10


class FuyaoError(Exception):
    """扶摇接口错误(配置缺失 / 网络失败 / 信封 code != 0)。"""

    def __init__(
        self,
        message: str = "",
        *,
        code: int | str | None = None,
        api_message: str = "",
        status: int | None = None,
        transport: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.api_message = api_message
        self.status = status
        self.transport = transport
        self.retry_after = retry_after


class FuyaoUnknownSymbolError(FuyaoError):
    """单标的历史接口明确拒绝当前代码;不代表整个数据源不可用。"""

    def __init__(self, symbol: str, message: str) -> None:
        super().__init__(message, code=1002, api_message=f"Unknown thscode: {symbol}")
        self.symbol = symbol


class FuyaoHistoryClient:
    """扶摇 REST 客户端 (线程安全: httpx.Client 可并发复用)。"""

    def __init__(
        self, api_key: str, base_url: str = BASE_URL, timeout: float = 20.0, *, exact: bool = False
    ) -> None:
        if not api_key:
            raise FuyaoError("未配置 FUYAO_API_KEY")
        self.exact = exact
        self.last_server_ts = 0  # 最近一页响应里的服务端时间戳(ms), 供行情归属
        self._http = httpx.Client(
            base_url=base_url,
            headers={"X-api-key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    # ---- 内部 ----
    def _get(self, path: str, params: dict) -> dict:
        """GET + 信封解包。code != 0 时抛 FuyaoError(含 code 与 message)。"""
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise FuyaoError(f"网络请求失败: {e}", transport=True) from e
        if resp.status_code != 200:
            delay = getattr(resp, "headers", {}).get("Retry-After", "")
            raise FuyaoError(
                f"HTTP {resp.status_code}: {path}",
                status=resp.status_code,
                retry_after=int(delay)
                if delay.isascii() and delay.isdigit() and len(delay) < 9
                else None,
            )
        try:
            payload = resp.json(parse_float=Decimal) if self.exact else resp.json()
        except ValueError as e:
            raise FuyaoError(f"响应不是 JSON: {path}") from e
        if not isinstance(payload, dict):
            raise FuyaoError("扶摇响应信封无效")
        code = payload.get("code")
        if self.exact and (type(code) not in (str, int) or "code" not in payload):
            raise FuyaoError("扶摇响应缺少有效状态码")
        if code not in (0, "0", None):
            message = payload.get("message", "")
            raise FuyaoError(
                f"扶摇接口错误 code={code}: {message} ({path})",
                code=code,
                api_message=message if isinstance(message, str) else "",
            )
        return payload.get("data") or {}

    def historical_kline(
        self,
        thscode: str,
        start_ms: int,
        end_ms: int,
        adjust: str = "none",
        *,
        checked: bool = False,
    ) -> list[dict]:
        """单标的日K(interval=1d 固定)。单次窗口 ≤10 年, 超出由调用方分片。

        adjust 必须显式传 "none" 取原始价 — 服务端默认是 forward(前复权),
        官方前复权序列事件间存在逐日漂移, 项目内禁止使用。
        返回 data.item 原始行: {date_ms, open_price, high_price, low_price,
        close_price, volume(股), turnover(元)}。
        """
        try:
            data = self._get(
                "/api/a-share/prices/historical",
                {
                    "thscode": thscode,
                    "interval": "1d",
                    "adjust": adjust,
                    "start": int(start_ms),
                    "end": int(end_ms),
                },
            )
        except FuyaoError as err:
            # 1002 也用于参数错误,只有明确匹配当前代码的响应才表示未覆盖。
            if str(err.code) == "1002" and err.api_message.strip() == f"Unknown thscode: {thscode}":
                raise FuyaoUnknownSymbolError(thscode, str(err)) from err
            raise
        if checked and (
            not isinstance(data, dict)
            or data.get("interval") != "1d"
            or data.get("adjust") != adjust
            or data.get("thscode", thscode) != thscode
            or not isinstance(data.get("item"), list)
        ):
            raise FuyaoError("单标的历史日K响应未确认代码、价格口径或行集")
        rows = data.get("item")
        return rows if isinstance(rows, list) else []

    def historical_kline_batch(
        self, thscodes: list[str], start_ms: int, end_ms: int
    ) -> dict[str, list[dict]]:
        """股票原始日K,每次最多10只。空组保留,缺组由 provider 单独补查。

        批量默认前复权,必须同时锁定请求与响应的 none/1d 契约。
        拒绝异常分组或记录,避免把错资产、错窗口或复权价写入原始日K。
        """
        if not thscodes:
            return {}
        if len(thscodes) > HISTORICAL_BATCH_LIMIT:
            raise FuyaoError("批量历史日K每次最多10个代码")
        data = self._get(
            "/api/a-share/prices/historical-batch",
            {
                "thscodes": ",".join(thscodes),
                "interval": "1d",
                "adjust": "none",
                "start": int(start_ms),
                "end": int(end_ms),
            },
        )
        if (
            not isinstance(data, dict)
            or data.get("interval") != "1d"
            or data.get("adjust") != "none"
        ):
            raise FuyaoError("批量历史日K响应未确认不复权日线口径")
        groups = data.get("item")
        if not isinstance(groups, list):
            raise FuyaoError("批量历史日K缺少有效分组")
        required = {
            "date_ms",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "turnover",
        }
        wanted = set(thscodes)
        out: dict[str, list[dict]] = {}
        for group in groups:
            if not isinstance(group, dict):
                raise FuyaoError("批量历史日K分组格式异常")
            symbol, bars = group.get("thscode"), group.get("item")
            if (
                not isinstance(symbol, str)
                or symbol not in wanted
                or symbol in out
                or not isinstance(bars, list)
            ):
                raise FuyaoError("批量历史日K返回未知、重复或无效分组")
            for bar in bars:
                if not isinstance(bar, dict) or not required.issubset(bar):
                    raise FuyaoError(f"批量历史日K记录缺少必要字段: {symbol}")
                timestamp = bar["date_ms"]
                if (
                    isinstance(timestamp, bool)
                    or not isinstance(timestamp, int)
                    or not start_ms <= timestamp <= end_ms
                ):
                    raise FuyaoError(f"批量历史日K日期无效或超出请求窗口: {symbol}")
            out[symbol] = bars
        return out
