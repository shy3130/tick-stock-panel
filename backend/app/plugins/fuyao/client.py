"""扶摇(同花顺金融数据 API) HTTP 客户端。

职责: 认证、统一信封解包、快照分页、单标的/批量日K、市场 dump 下载。不知道 provider / services 层。
文档: https://fuyao.aicubes.cn/docs — REST + X-api-key, 响应信封 {code, message, data}。

时间字段口径: 所有 *ms 字段(含 start/end 入参与 date_ms/ex_date_ms 出参)均为
北京时间零点对应的 epoch ms(= UTC 前一日 16:00), 由 provider 层统一 +8h 换算。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://fuyao.aicubes.cn"
HISTORICAL_BATCH_LIMIT = 10

# 单页 6000 覆盖全市场(实测 ~5600 含北交所, 2026-08 服务端不截断 limit=6000),
# 一次请求拉完; 分页循环兜底未来标的扩容或服务端改为截断的场景。
_SNAPSHOT_PAGE_SIZE = 6000
_SNAPSHOT_MAX_PAGES = 50
_PAGE_INTERVAL_S = 0.15  # 页间隔, 降低触发限频 (code=4001) 的概率


class FuyaoError(Exception):
    """扶摇接口错误(配置缺失 / 网络失败 / 信封 code != 0)。"""

    def __init__(self, message: str = "", *, code: int | str | None = None, api_message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.api_message = api_message


class FuyaoUnknownSymbolError(FuyaoError):
    """单标的历史接口明确拒绝当前代码;不代表整个数据源不可用。"""

    def __init__(self, symbol: str, message: str) -> None:
        super().__init__(message, code=1002, api_message=f"Unknown thscode: {symbol}")
        self.symbol = symbol


class FuyaoClient:
    """扶摇 REST 客户端 (线程安全: httpx.Client 可并发复用)。"""

    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 20.0) -> None:
        if not api_key:
            raise FuyaoError("未配置 FUYAO_API_KEY")
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
            raise FuyaoError(f"网络请求失败: {e}") from e
        if resp.status_code != 200:
            raise FuyaoError(f"HTTP {resp.status_code}: {path}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise FuyaoError(f"响应不是 JSON: {path}") from e
        code = payload.get("code")
        if code not in (0, "0", None):
            message = payload.get("message", "")
            raise FuyaoError(f"扶摇接口错误 code={code}: {message} ({path})",
                             code=code, api_message=message if isinstance(message, str) else "")
        return payload.get("data") or {}

    # ---- 快照 ----
    def snapshot_page(
        self, limit: int = _SNAPSHOT_PAGE_SIZE, offset: int = 0
    ) -> tuple[list[dict], int]:
        """拉取一页 A 股全市场快照。返回 (rows, total), total 为全市场总数。

        实测响应(2026-08): data={timestamp, total, item}; 官方文档示例为
        data={count, data}。两者都兼容, 以实测为准。
        """
        data = self._get("/api/a-share/prices/snapshot", {"limit": limit, "offset": offset})
        try:
            self.last_server_ts = int(data.get("timestamp") or 0)
        except (TypeError, ValueError):
            self.last_server_ts = 0
        rows = data.get("item")
        if not isinstance(rows, list):
            rows = data.get("data") if isinstance(data.get("data"), list) else []
        raw_total = data.get("total")
        if raw_total is None:
            raw_total = data.get("count") or 0
        try:
            total = int(raw_total or 0)
        except (TypeError, ValueError):
            total = 0
        return rows, total

    def snapshot_all(self) -> tuple[list[dict], int]:
        """分页拉取全市场快照。返回 (rows, 服务端时间戳ms)。

        服务端时间戳用于行情归属; 缺失时返回 0, 由调用方退回本地时间。
        空数据 / 中途失败时抛 FuyaoError。
        """
        out: list[dict] = []
        server_ts = 0
        offset = 0
        for page in range(_SNAPSHOT_MAX_PAGES):
            if page > 0:
                time.sleep(_PAGE_INTERVAL_S)
            rows, total = self.snapshot_page(offset=offset)
            if not rows:
                break
            out.extend(rows)
            if not server_ts:
                server_ts = self.last_server_ts
            if total and len(out) >= total:
                break
            offset += len(rows)
        if not out:
            raise FuyaoError("全市场快照为空")
        return out, server_ts

    # ---- 指数快照 ----
    def index_snapshot(self, thscodes: list[str]) -> tuple[list[dict], int]:
        """拉取指数行情快照 (/api/a-share-index/prices/snapshot)。返回 (rows, 服务端时间戳ms)。

        与 A 股快照不同: 必须显式传 thscodes (逗号分隔), 无全量枚举;
        单次批量上限实测 627 个代码 (~6.3KB 参数, 超出 HTTP 400);
        混入未知代码整批失败 (code=1002 连坐), 调用方需自行过滤。
        覆盖范围: 沪深交易所指数 + 同花顺板块指数, 无北交所 (官方文档明确)。
        """
        if not thscodes:
            return [], 0
        joined = ",".join(thscodes[:627])
        data = self._get("/api/a-share-index/prices/snapshot", {"thscodes": joined})
        try:
            server_ts = int(data.get("timestamp") or 0)
        except (TypeError, ValueError):
            server_ts = 0
        rows = data.get("item")
        if not isinstance(rows, list):
            rows = []
        return rows, server_ts

    # ---- 历史日K ----
    def historical_kline(
        self, thscode: str, start_ms: int, end_ms: int, adjust: str = "none"
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
        data = self._get("/api/a-share/prices/historical-batch", {
            "thscodes": ",".join(thscodes), "interval": "1d", "adjust": "none",
            "start": int(start_ms), "end": int(end_ms),
        })
        if not isinstance(data, dict) or data.get("interval") != "1d" or data.get("adjust") != "none":
            raise FuyaoError("批量历史日K响应未确认不复权日线口径")
        groups = data.get("item")
        if not isinstance(groups, list):
            raise FuyaoError("批量历史日K缺少有效分组")
        required = {"date_ms", "open_price", "high_price", "low_price", "close_price", "volume", "turnover"}
        wanted = set(thscodes)
        out: dict[str, list[dict]] = {}
        for group in groups:
            if not isinstance(group, dict):
                raise FuyaoError("批量历史日K分组格式异常")
            symbol, bars = group.get("thscode"), group.get("item")
            if not isinstance(symbol, str) or symbol not in wanted or symbol in out or not isinstance(bars, list):
                raise FuyaoError("批量历史日K返回未知、重复或无效分组")
            for bar in bars:
                if not isinstance(bar, dict) or not required.issubset(bar):
                    raise FuyaoError(f"批量历史日K记录缺少必要字段: {symbol}")
                timestamp = bar["date_ms"]
                if isinstance(timestamp, bool) or not isinstance(timestamp, int) or not start_ms <= timestamp <= end_ms:
                    raise FuyaoError(f"批量历史日K日期无效或超出请求窗口: {symbol}")
            out[symbol] = bars
        return out

    # ---- 财务 ----
    # 端点均单标的(thscode 不接受逗号)。取数模式二选一: limit=最近N期 或 start/end 区间,
    # 这里只用 limit。period=quarterly 覆盖每个季度末(含年报期), 与项目"各报告期累积"口径一致。
    _STATEMENT_ENDPOINTS = {
        "income": "income-statements",
        "balance_sheet": "balance-sheets",
        "cash_flow": "cash-flow-statements",
    }

    def financial_statements(
        self, stmt: str, thscode: str, limit: int = 1
    ) -> list[dict]:
        """单标的财务报表多期序列。stmt: income | balance_sheet | cash_flow。

        返回 data.item 原始行: 共有元数据(thscode/period/fiscal_year/fiscal_period/
        report_date_ms/period_end_ms/currency) + 各表字段。行内 null 表示该期未披露。
        """
        endpoint = self._STATEMENT_ENDPOINTS.get(stmt)
        if endpoint is None:
            raise FuyaoError(f"未知财务报表类型: {stmt}")
        data = self._get(
            f"/api/a-share/financials/{endpoint}",
            {"thscode": thscode, "period": "quarterly", "limit": max(1, min(20, limit))},
        )
        rows = data.get("item")
        return rows if isinstance(rows, list) else []

    def financial_indicators(self, thscode: str, report: str) -> list[dict]:
        """单标的单报告期财务指标(report 格式 yyyy-N, N=1..4 对应一季报..年报)。

        返回 data.abilities 原始列表 [{ability, indicators: [{index_id, value}]}];
        value 为保留原始精度的数值字符串(百分制指标即百分点数), 缺失为 null。
        未披露报告期实测返回 code=5003(文档写 3002, 以实测为准) → 经 _get 抛 FuyaoError,
        由调用方按"该期无数据"处理。
        """
        data = self._get(
            "/api/a-share/financials/indicators",
            {"thscode": thscode, "report": report},
        )
        abilities = data.get("abilities")
        return abilities if isinstance(abilities, list) else []

    def valuations_snapshot(self, thscodes: list[str]) -> list[dict]:
        """批量估值快照(pe_ttm/pe_mrq/pb_mrq/ps_ttm/pcf_ttm), 数值为最新口径。

        服务端单次上限 100 只(超出 code=1003), 分批由调用方负责。
        返回 data.item 原始行。
        """
        data = self._get(
            "/api/a-share/valuations/snapshot",
            {"thscodes": ",".join(thscodes[:100])},
        )
        rows = data.get("item")
        return rows if isinstance(rows, list) else []

    def price_snapshot_batch(self, thscodes: list[str]) -> list[dict]:
        """按 thscodes 批量行情快照(最新价等), 用于估值推导的分母。

        与全市场分页快照同一端点; thscodes 显式传入时不分页。
        返回 data.item 原始行。
        """
        data = self._get(
            "/api/a-share/prices/snapshot",
            {"thscodes": ",".join(thscodes[:100])},
        )
        rows = data.get("item")
        return rows if isinstance(rows, list) else []

    def trading_days(self) -> list[dict]:
        """近一年 A 股交易日序列 (固定窗口 [今日-1年, 今日], 无入参)。

        返回 data.item 原始行: {date_ms(上海零点), date(yyyyMMdd)}。
        供交易日探针判定「今天在列表内 ⇔ 交易日」。
        """
        data = self._get("/api/a-share/calendar/trading-days", {})
        rows = data.get("item")
        return rows if isinstance(rows, list) else []

    def dragon_tiger_list(self, board_type: str = "all", date: str | None = None) -> dict:
        """龙虎榜榜单 (特色数据)。board_type: all | org | hot_money。

        返回 data 原始容器: {trade_date, count, stock_count, stock_items[],
        hot_money_items[]}。省略 date 时服务端自动取最近已发布交易日;
        显式传非交易日返回 code=1002 (由调用方做交易日回退)。
        实测字段(2026-08): stock_items 12 个基础字段, org 榜额外带 4 个机构字段;
        文档中的 limit_reason / amount 实际不返回。
        """
        params: dict = {"board_type": board_type}
        if date:
            params["date"] = date
        return self._get("/api/a-share/special-data/dragon-tiger-list", params)

    def short_term_benchmark(self, date: str | None = None) -> dict:
        """短线风向标竞价基准 (同花顺竞价筛选, 每日约 5~6 只)。

        返回 data 原始容器: {date, date_ms, item[]}。item 行:
        {thscode, ticker, name, auction_pct, tags[]}。支持一年内历史日期;
        显式传非交易日返回 code=1002 (由调用方做交易日回退)。
        """
        params: dict = {}
        if date:
            params["date"] = date
        return self._get("/api/a-share/auction/short-term-benchmark", params)

    # ---- 市场 dump ----
    def dump_download_url(self, dump_kind: str) -> dict:
        """获取 dump 预签名下载信息(约 300s 有效)。

        dump_kind: adjustment-factors | daily-k-10d | daily-k。
        返回 {presigned_url, presigned_url_expires_at, expires_in_seconds};
        release 版本号(如 20260828)嵌在 presigned_url 的 releases/<date>/ 路径中,
        供调用方做缓存版本管理。
        """
        return self._get(f"/api/dump/market-dumps/{dump_kind}/download-url", {})

    def download_dump(self, dump_kind: str, dest: Path) -> Path:
        """下载 dump 到 dest(先写 .part 临时文件, 成功后原子改名)。失败抛 FuyaoError。

        预签名 URL 指向对象存储, 请求不得携带 X-api-key 头 → 用独立裸请求,
        不经过持有认证头的 self._http。
        """
        url = str(self.dump_download_url(dump_kind).get("presigned_url") or "")
        if not url:
            raise FuyaoError(f"dump {dump_kind} 未返回预签名 URL")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    raise FuyaoError(f"dump {dump_kind} 下载失败 HTTP {resp.status_code}")
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        fh.write(chunk)
            tmp.replace(dest)
        except httpx.HTTPError as e:
            raise FuyaoError(f"dump {dump_kind} 下载网络失败: {e}") from e
        finally:
            tmp.unlink(missing_ok=True)
        return dest
