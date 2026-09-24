"""扶摇(同花顺金融数据 API)内置数据源 provider。

方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

实现数据集:
  - realtime     A 股全市场快照 (分页)
  - daily        A 股日K, 原始价; 近端窗口走 daily-k-10d 全市场 dump(1 次请求),
                 深窗口走单标的 historical 接口(≤10 年/次自动分片)
  - adj_factor   A 股除权因子; adjustment-factors 事件 dump + 自家原始日K前收盘,
                 按交易所公式推导单事件比值, 涨跌停自检
  - financial    财务五表(股本除外): 三表多期序列 + 指标单期, 字段映射为 TickFlow
                 canonical 列名, 扶摇独有字段原名透传为扩展列; bps 由估值 pb_mrq
                 反推; shares 无上游接口恒空
未声明 minute → provider_has_dataset 为 False, 自动回退 tickflow。

单位与口径 (CONTRIBUTING §3.1, 不可凭字段名推断):
  - 扶摇 price_change_ratio_pct 为百分数数值 (1.74 = +1.74%), 本项目 realtime
    change_pct 契约为小数制 (0.0174 = 1.74%) → 此处显式 / 100。
  - 扶摇 volume 单位为股, 本项目日K/实时契约均为手 → 统一 floor(股/100)。
  - turnover 单位元, 与内部一致, 直接透传。
  - 日K取数 adjust=none 锁定: 官方 forward 序列事件间有逐日漂移(2026-08 实测),
    项目内前复权一律由 indicators.pipeline 用本地因子计算。
  - ex_factor 为单事件比值(非累积), 累积链由 pipeline._apply_adj_factor 构建。
"""

from __future__ import annotations

import calendar
import contextlib
import logging
import math
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from app.data_providers.normalizer import DAILY_COLS, normalize_daily
from app.indicators.pipeline import filter_halt_days
from app.plugins.fuyao import client as fuyao_client
from app.plugins.fuyao.client import FuyaoClient, FuyaoError

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余数据集 provider_has_dataset 返回 False → 回退 tickflow
_DATASETS = ("realtime", "daily", "adj_factor", "financial")

API_KEY_ENV = "FUYAO_API_KEY"
SECRETS_FIELD = "fuyao_api_key"  # UI 配置的 Key 存 secrets.json, 优先级高于 .env

# 扶摇 *ms 时间字段为北京时间零点(= UTC 前一日 16:00), +8h 后按 UTC 解析即得交易日
_SH_MS = 28_800_000
_HIST_MAX_SPAN_MS = 3650 * 86_400_000  # historical 单次窗口上限 10 年, 超出由本层分片
_HIST_INTERVAL_S = 0.12  # 单标的请求节流(实测 200+ 连发未触发 4001 限频)
_FINANCIAL_HISTORY_PERIODS = 8  # 财务首装全量历史: 最近 8 期季报(约 2 年)
_VALUATION_BATCH = 100  # 估值/价格快照端点单次上限 100 只
# 项目财务表名 → 扶摇报表端点名
_STATEMENT_ENDPOINTS = {
    "income": "income-statements",
    "balance_sheet": "balance-sheets",
    "cash_flow": "cash-flow-statements",
}
_ADJ_DUMP_KIND = "adjustment-factors"
_DAILY10_DUMP_KIND = "daily-k-10d"
_DAILY_DUMP_KIND = "daily-k"  # 10 年全量日K dump(约 172MB), 深窗口一次下载覆盖全市场
_RECENT_DUMP_DAYS = 12  # 窗口跨度 ≤ 此天数时优先走 10d dump(覆盖 ≈10 个交易日)
_PREV_CLOSE_BACKDAYS = 30  # 推导因子时向前找"除权日前收盘"的回看天数(容忍长期停牌)
_DAILY_DUMP_BATCH_ROWS = 100_000
_HIST_SYMBOL_BATCH = 50
_DAILY_DUMP_COLUMNS = [
    "thscode", "adjusted", "date_ms", "open_price", "high_price", "low_price",
    "close_price", "volume", "turnover",
]


def get_api_key() -> str:
    from app import secrets_store

    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def availability() -> tuple[bool, str]:
    """loader 启动自检: API Key 已配置(secrets.json 或 .env)才注册为可切换数据源。不抛异常。"""
    if get_api_key():
        return True, "ok"
    # 状态行会拼在「未配置」标签之后, 文案不再重复"未配置"字样
    return False, f"缺少 API Key(可在下方输入框直接填写,或配置环境变量 {API_KEY_ENV})"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Key 实探一次快照接口(先探后存, 对齐 /tickflow-key 语义)。不落盘。"""
    client = None
    try:
        client = fuyao_client.FuyaoClient(api_key=api_key, timeout=10.0)
        client.snapshot_page(limit=1)
        return True, "ok"
    except FuyaoError as e:
        return False, f"Key 无效或网络失败: {e}"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


@dataclass
class _FuyaoConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "fuyao"
    display_name: str = "fuyao"
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


def _first(row: dict, *names: str):
    """按优先级取第一个非 None 字段。实测字段名与官方文档示例不一致, 两者兼容。"""
    for n in names:
        if row.get(n) is not None:
            return row.get(n)
    return None


def _date_of_ms(value) -> date | None:
    """扶摇 *ms(上海零点) → 交易日。None/非法值返回 None, 不伪造。"""
    if value is None:
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp((ms + _SH_MS) // 1000, tz=UTC).date()


def _ms_of_date(d: date) -> int:
    """交易日 → 扶摇 start/end 入参口径的 ms(该日上海零点的 epoch ms, 不依赖本机时区)。"""
    return (calendar.timegm(d.timetuple()) - 28_800) * 1000


def _iso_of_ms(value) -> str | None:
    """扶摇 *ms → ISO 日期字符串(项目财务表 period_end/announce_date 的存储口径)。"""
    d = _date_of_ms(value)
    return d.isoformat() if d is not None else None


def _report_quarter(fiscal_period) -> int | None:
    """fiscal_period(Q1..Q4/FY) → 指标接口 report 参数的季号 N(1..4)。"""
    if not fiscal_period:
        return None
    text = str(fiscal_period).strip().upper()
    if text == "FY":
        return 4
    try:
        return int(text.lstrip("Q"))
    except ValueError:
        return None


def _ref_price(
    prev_close: float, dividend: float, bonus: float, allot: float, allot_price: float
) -> float | None:
    """交易所除权参考价: (P - D + AR·AP) / (1 + S + AR), 四舍五入(half-up)保留 2 位。

    half-up 是交易所口径; 银行家舍入会让约半数事件在第 2 位小数上偏离(对拍实证)。
    """
    denom = 1.0 + bonus + allot
    if denom <= 0:
        return None
    x = (prev_close - dividend + allot * allot_price) / denom
    return math.floor(x * 100 + 0.5) / 100


def _price_limit(symbol: str) -> float:
    """按代码前缀给涨跌停幅度(自检容差用): 创业板/科创板 20%, 北交所 30%, 主板 10%。"""
    code = symbol.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def _release_of(url: str) -> str:
    """从预签名 URL 提取 release 版本号(releases/<date>/ 路径), 提不到返回 unknown。"""
    m = re.search(r"releases/(\d+)/", url or "")
    return m.group(1) if m else "unknown"


def _cache_dir() -> Path:
    from app.config import settings

    d = settings.data_dir / "cache" / "fuyao"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _kline_rows(symbol: str, bars: list[dict]) -> list[dict]:
    """historical/dump 原始行(价格元, volume 股) → 内部行(volume 手)。"""
    out = []
    for b in bars:
        v = _to_float(b.get("volume"))
        out.append(
            {
                "symbol": symbol,
                "date": _date_of_ms(b.get("date_ms")),
                "open": _to_float(b.get("open_price")),
                "high": _to_float(b.get("high_price")),
                "low": _to_float(b.get("low_price")),
                "close": _to_float(b.get("close_price")),
                "volume": math.floor(v / 100.0) if v is not None else None,
                "amount": _to_float(b.get("turnover")),
            }
        )
    return out


def _tail_ok(end_d: date, covered_max: date) -> bool:
    """请求终点是否被覆盖到 covered_max: 周末/节假日的自然缺口(≤3 天)不算缺失。"""
    if end_d <= covered_max:
        return True
    return (end_d - covered_max).days <= 3 and end_d.weekday() >= 5


def _dump_covers(dump: pl.DataFrame, start_d: date, end_d: date) -> bool:
    """dump 日期范围是否覆盖请求窗口: 起点必须落在 dump 内; 终点允许周末自然缺口。"""
    if dump.is_empty() or "date_ms" not in dump.columns:
        return False
    dates = pl.from_epoch(dump["date_ms"].cast(pl.Int64) + _SH_MS, time_unit="ms").dt.date()
    dmin, dmax = dates.min(), dates.max()
    return start_d >= dmin and _tail_ok(end_d, dmax)


def _dump_date_range(path: Path) -> tuple[date | None, date | None]:
    """lazy 读 parquet 的 date_ms 边界(走元数据/少量行组, 不整读大文件)。"""
    row = (
        pl.scan_parquet(path)
        .select(
            pl.from_epoch(pl.col("date_ms").min() + _SH_MS, time_unit="ms").dt.date().alias("dmin"),
            pl.from_epoch(pl.col("date_ms").max() + _SH_MS, time_unit="ms").dt.date().alias("dmax"),
        )
        .collect()
    )
    return row["dmin"][0], row["dmax"][0]


def _map_snapshot_row(row: dict, fetched_ms: int, *, volume_to_hand: bool = True) -> dict | None:
    """扶摇快照行 → 内部 realtime record。字段缺失时按依赖推导, 不伪造数据。

    实测字段(2026-08): high_price / low_price / prev_price;
    官方文档示例: highest_price / lowest_price / prev_close_price。两者都取。
    volume_to_hand: A 股快照 volume 为股 → 手; 指数快照无此口径, 直接透传。
    """
    symbol = row.get("thscode")
    if not symbol:
        return None
    last = _to_float(row.get("last_price"))
    prev = _to_float(_first(row, "prev_price", "prev_close_price"))

    # 百分数 (1.74 = +1.74%) → 小数制 (0.0174), 契约见模块 docstring
    pct = _to_float(row.get("price_change_ratio_pct"))
    change_pct = pct / 100.0 if pct is not None else None

    change_amount = _to_float(row.get("price_change"))
    if change_amount is None and last is not None and prev is not None:
        change_amount = last - prev
    if change_pct is None and change_amount is not None and prev not in (None, 0):
        # 与 quote_service 的推导同口径: 小数制, 不乘 100
        change_pct = change_amount / prev

    volume = _to_float(row.get("volume"))

    return {
        "symbol": symbol,
        "name": row.get("name"),  # 快照无名称, 由下游维表关联
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(row.get("open_price")),
        "high": _to_float(_first(row, "high_price", "highest_price")),
        "low": _to_float(_first(row, "low_price", "lowest_price")),
        "volume": math.floor(volume / 100.0) if (volume is not None and volume_to_hand) else volume,
        "amount": _to_float(row.get("turnover")),
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": None,  # 快照未提供, 不启发式计算
        "turnover_rate": None,  # 需股本口径 (§3.4), 交给 enriched 管道用历史股本计算
        "timestamp": fetched_ms,
        "session": None,
    }


class FuyaoProvider:
    """扶摇数据源。realtime = A 股全市场快照(quote_service 全市场模式轮询调用)。"""

    name = "fuyao"
    builtin = True

    def __init__(self) -> None:
        self.config = _FuyaoConfig()
        self._client: FuyaoClient | None = None
        self._dump_memo: dict[str, pl.DataFrame] = {}
        self._dump_path_memo: dict[str, Path] = {}

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None
        self._dump_memo.clear()
        self._dump_path_memo.clear()

    def _get_client(self) -> FuyaoClient:
        if self._client is None:
            self._client = fuyao_client.FuyaoClient(api_key=get_api_key())
        return self._client

    # ---- dump 缓存 ----
    def _ensure_dump_path(self, dump_kind: str, cache_prefix: str) -> Path:
        """确保最新 release 的 dump 已落盘, 返回缓存路径(大文件不整读进内存)。

        release 号取自预签名 URL 的 releases/<date>/ 路径; 新 release 落盘后清理旧版缓存。
        """
        memo = self._dump_path_memo.get(dump_kind)
        if memo is not None and memo.exists():
            return memo
        client = self._get_client()
        info = client.dump_download_url(dump_kind)
        release = _release_of(str(info.get("presigned_url") or ""))
        dest = _cache_dir() / f"{cache_prefix}__{release}.parquet"
        if not dest.exists():
            client.download_dump(dump_kind, dest)
            for old in dest.parent.glob(f"{cache_prefix}__*.parquet"):
                if old.name != dest.name:
                    old.unlink(missing_ok=True)
            logger.info("扶摇 dump %s(release %s)已下载: %s", dump_kind, release, dest.name)
        self._dump_path_memo[dump_kind] = dest
        return dest

    def _ensure_dump(self, dump_kind: str, cache_prefix: str) -> pl.DataFrame:
        """小体量 dump(快照 10d / 因子)整读 + 进程内 memo, 避免重复打接口/读盘。"""
        memo = self._dump_memo.get(dump_kind)
        if memo is not None:
            return memo
        df = pl.read_parquet(self._ensure_dump_path(dump_kind, cache_prefix))
        self._dump_memo[dump_kind] = df
        return df

    def _ensure_daily_big_dump(self, start_d: date) -> Path | None:
        """10 年全量日K dump(约 172MB)。只要求覆盖窗口起点; 末端缺口由 10d dump 补。

        已有缓存覆盖起点就直接复用, 不追新 release(避免深窗口高频触发时日日重下
        172MB — 旧 release 的中段历史不会变, 尾部新鲜度交给 1MB 的 10d dump)。
        """
        for f in sorted(_cache_dir().glob("daily_k__*.parquet"), reverse=True):
            try:
                dmin, _ = _dump_date_range(f)
            except Exception:  # 缓存损坏不致命, 换下一个/重拉
                continue
            if dmin is not None and start_d >= dmin:
                return f
        try:
            path = self._ensure_dump_path(_DAILY_DUMP_KIND, "daily_k")
        except FuyaoError as e:
            logger.warning("扶摇 10 年 dump 不可用, 回退单标的接口: %s", e)
            return None
        dmin, _ = _dump_date_range(path)
        if dmin is None or start_d < dmin:
            return None  # 窗口比 10 年更早 → 单标的兜底
        return path

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → 内部 realtime records。失败软返回空列表(不阻断轮询)。"""
        try:
            rows, server_ts = self._get_client().snapshot_all()
        except FuyaoError as e:
            logger.warning("扶摇实时行情拉取失败: %s", e)
            return []

        # 优先用服务端时间戳(行情归属); 缺失时退回本地时间
        fetched_ms = server_ts or int(time.time() * 1000)

        records = []
        dropped = 0
        for row in rows:
            rec = _map_snapshot_row(row, fetched_ms)
            if rec is not None:
                records.append(rec)
            else:
                dropped += 1
        if dropped and not records:
            # 整页都识别不出 thscode → 大概率接口 schema 变了, 明确告警而非静默空数据
            logger.warning("扶摇快照 %d 行全部缺少 thscode 字段, 疑似接口结构变化", dropped)
            return []
        logger.info("扶摇实时行情拉取完成: %d 条(丢弃 %d 行)", len(records), dropped)
        return records

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """指数实时快照 → 内部 realtime record (可选插件协议, quote_service 鸭子类型调用)。

        A 股快照不含指数, 指数在扶摇是独立端点; 覆盖沪深交易所指数 + 同花顺板块,
        无北交所 (未知代码会整批 1002 连坐, .BJ 直接跳过)。失败返回 None,
        让上层与“成功但无数据”的空列表区分, 保留上轮有效指数缓存。
        """
        wanted = [s for s in symbols if s and not s.upper().endswith(".BJ")]
        if not wanted:
            return []
        try:
            rows, server_ts = self._get_client().index_snapshot(wanted)
        except FuyaoError as e:
            logger.warning("扶摇指数行情拉取失败: %s", e)
            return None

        fetched_ms = server_ts or int(time.time() * 1000)
        records = []
        for row in rows:
            rec = _map_snapshot_row(row, fetched_ms, volume_to_hand=False)
            if rec is not None:
                records.append(rec)
        logger.info("扶摇指数行情拉取完成: %d 条(请求 %d 只)", len(records), len(wanted))
        return records

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股日K → 内部契约。原始价(adjust=none 锁定)、volume 股→手(floor /100)。

        取数三档(逐级降级, 全部失败才空手而归):
        - 近端窗口(跨度 ≤12 天): daily-k-10d dump(约 1MB), 1 次请求覆盖全部标的;
        - 深窗口: daily-k 10 年全量 dump(约 172MB, 缓存覆盖起点即复用, 不追新 release),
          末端缺口由 10d dump 补尾 — 全市场深回填从"逐票 34 分钟"降为"一次下载+秒级筛选";
        - 兜底: 单标的 historical 接口(窗口早于 dump 覆盖 / dump 不可用; 10 年自动分片,
          逐标的节流 + 进度回调)。
        """
        chunks = [
            df
            for df in self.iter_daily(
                symbols,
                start_time=start_time,
                end_time=end_time,
                asset_type=asset_type,
                on_chunk_done=on_chunk_done,
            )
            if not df.is_empty()
        ]
        return pl.concat(chunks, how="diagonal_relaxed") if chunks else pl.DataFrame()

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """分批产出日K,供历史同步逐批落盘,避免全市场结果累积在内存。"""
        if not symbols or asset_type != "stock":
            return
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=365))
        start_d, end_d = start_dt.date(), end_dt.date()
        symset = set(symbols)

        if (end_d - start_d).days <= _RECENT_DUMP_DAYS:
            try:
                dump = self._ensure_dump(_DAILY10_DUMP_KIND, "daily_k_10d")
                if _dump_covers(dump, start_d, end_d):
                    df = self._daily_from_dump(dump, symset, start_d, end_d)
                    if on_chunk_done:
                        on_chunk_done(1, 1)
                    if not df.is_empty():
                        yield df
                    return
            except FuyaoError as e:
                logger.warning("扶摇 10d dump 不可用: %s", e)

        dump_info = self._daily_dump_info()
        sources: list[tuple[str, date, date]] = []
        if dump_info:
            _, dump_min, dump_max = dump_info
            if start_d < dump_min:
                sources.append(("api", start_d, min(end_d, dump_min - timedelta(days=1))))
            overlap_start, overlap_end = max(start_d, dump_min), min(end_d, dump_max)
            if overlap_start <= overlap_end:
                sources.append(("dump", overlap_start, overlap_end))
            tail_start = max(start_d, dump_max + timedelta(days=1))
            if tail_start <= end_d and not _tail_ok(end_d, dump_max):
                try:
                    ten = self._ensure_dump(_DAILY10_DUMP_KIND, "daily_k_10d")
                    ten_dates = pl.from_epoch(
                        ten["date_ms"].cast(pl.Int64) + _SH_MS, time_unit="ms"
                    ).dt.date()
                    ten_min, ten_max = ten_dates.min(), ten_dates.max()
                except (FuyaoError, KeyError):
                    ten_min = ten_max = None
                if (
                    ten_min is not None
                    and ten_min <= dump_max + timedelta(days=1)
                    and _tail_ok(end_d, ten_max)
                ):
                    sources.append(("10d", tail_start, end_d))
                else:
                    # 多年 dump 与请求终点之间存在不可验证的缺口,不能返回半段数据。
                    sources = [("api", start_d, end_d)]
        else:
            sources.append(("api", start_d, end_d))

        dump_batch_count = 0
        if dump_info:
            dump_rows = pq.ParquetFile(dump_info[0]).metadata.num_rows
            dump_batch_count = max(
                1, (dump_rows + _DAILY_DUMP_BATCH_ROWS - 1) // _DAILY_DUMP_BATCH_ROWS
            )
        api_batch_count = (len(symbols) + _HIST_SYMBOL_BATCH - 1) // _HIST_SYMBOL_BATCH
        total = sum(
            dump_batch_count if kind == "dump" else 1 if kind == "10d" else api_batch_count
            for kind, _, _ in sources
        )
        done = 0
        for kind, source_start, source_end in sources:
            if source_start > source_end:
                continue
            if kind == "dump":
                path = dump_info[0]  # type: ignore[index]
                for df in self._iter_big_dump(path, symset, source_start, source_end):
                    done += 1
                    if on_chunk_done:
                        on_chunk_done(done, total)
                    if not df.is_empty():
                        yield df
            elif kind == "10d":
                ten = self._ensure_dump(_DAILY10_DUMP_KIND, "daily_k_10d")
                df = self._daily_from_dump(ten, symset, source_start, source_end)
                done += 1
                if on_chunk_done:
                    on_chunk_done(done, total)
                if not df.is_empty():
                    yield df
            else:
                batches = [
                    symbols[i:i + _HIST_SYMBOL_BATCH]
                    for i in range(0, len(symbols), _HIST_SYMBOL_BATCH)
                ]
                for batch in batches:
                    rows: list[dict] = []
                    for symbol in batch:
                        rows.extend(_kline_rows(
                            symbol,
                            self._historical_bars(symbol, source_start, source_end),
                        ))
                        time.sleep(_HIST_INTERVAL_S)
                    df = normalize_daily(rows, source=self.name)
                    done += 1
                    if on_chunk_done:
                        on_chunk_done(done, total)
                    if not df.is_empty():
                        yield df

    def _daily_dump_info(self) -> tuple[Path, date, date] | None:
        """返回多年 dump 的路径和覆盖范围,不把大文件读入进程内存。"""
        path = None
        for candidate in sorted(_cache_dir().glob("daily_k__*.parquet"), reverse=True):
            try:
                dmin, dmax = _dump_date_range(candidate)
            except Exception:
                continue
            if dmin is not None and dmax is not None:
                return candidate, dmin, dmax
        try:
            path = self._ensure_dump_path(_DAILY_DUMP_KIND, "daily_k")
            dmin, dmax = _dump_date_range(path)
        except FuyaoError as e:
            logger.warning("扶摇 10 年 dump 不可用, 回退单标的接口: %s", e)
            return None
        return (path, dmin, dmax) if dmin is not None and dmax is not None else None

    def _daily_from_dump(
        self, dump: pl.DataFrame, symset: set[str], start_d: date, end_d: date
    ) -> pl.DataFrame:
        df = dump.with_columns(
            pl.from_epoch(pl.col("date_ms") + _SH_MS, time_unit="ms").dt.date().alias("date")
        )
        return self._map_daily_dump(df, symset, start_d, end_d)

    def _iter_big_dump(
        self, path: Path, symset: set[str], start_d: date, end_d: date
    ) -> Iterator[pl.DataFrame]:
        """按固定 record batch 读取多年 dump,不做单次全量 collect。"""
        parquet = pq.ParquetFile(path)
        columns = [name for name in _DAILY_DUMP_COLUMNS if name in parquet.schema.names]
        for batch in parquet.iter_batches(
            batch_size=_DAILY_DUMP_BATCH_ROWS,
            columns=columns,
        ):
            raw = pl.from_arrow(batch)
            if raw.is_empty() or "date_ms" not in raw.columns or "thscode" not in raw.columns:
                yield pl.DataFrame()
                continue
            start_ms, end_ms = _ms_of_date(start_d), _ms_of_date(end_d)
            raw = raw.filter(
                (pl.col("date_ms") >= start_ms)
                & (pl.col("date_ms") <= end_ms)
                & pl.col("thscode").is_in(sorted(symset))
            )
            if raw.is_empty():
                yield pl.DataFrame()
                continue
            raw = raw.with_columns(
                pl.from_epoch(pl.col("date_ms") + _SH_MS, time_unit="ms").dt.date().alias("date")
            )
            yield self._map_daily_dump(raw, symset, start_d, end_d)

    def _map_daily_dump(
        self, df: pl.DataFrame, symset: set[str], start_d: date, end_d: date
    ) -> pl.DataFrame:
        """dump 原始行 → 内部日K契约(股→手、字段重命名、停牌过滤)。"""
        if "adjusted" in df.columns:
            df = df.filter(pl.col("adjusted") == "none")  # 防御: dump 变为复权口径时拒绝落库
        out = (
            df.filter(
                (pl.col("date") >= start_d)
                & (pl.col("date") <= end_d)
                & pl.col("thscode").is_in(sorted(symset))
            )
            .with_columns(
                (pl.col("volume") / 100.0).floor().alias("volume"),  # 股 → 手
                pl.col("thscode").alias("symbol"),
            )
            .rename(
                {
                    "open_price": "open",
                    "high_price": "high",
                    "low_price": "low",
                    "close_price": "close",
                    "turnover": "amount",
                }
            )
        )
        out = filter_halt_days(out)
        cols = [c for c in DAILY_COLS if c in out.columns]
        return out.select(cols).sort(["symbol", "date"]) if not out.is_empty() else out.select(cols)

    def _historical_bars(self, symbol: str, start_d: date, end_d: date) -> list[dict]:
        """按 ≤10 年窗口分片拉取单标的原始日K。中途失败软返回已得行, 不抛出。"""
        out: list[dict] = []
        s = _ms_of_date(start_d)
        e = _ms_of_date(end_d)
        while s <= e:
            chunk_end = min(e, s + _HIST_MAX_SPAN_MS - 1)
            try:
                out.extend(self._get_client().historical_kline(symbol, s, chunk_end, adjust="none"))
            except FuyaoError as err:
                logger.warning("扶摇日K拉取失败 %s [%s ~ %s]: %s", symbol, start_d, end_d, err)
                break
            if s + _HIST_MAX_SPAN_MS <= e:
                time.sleep(_HIST_INTERVAL_S)
            s = chunk_end + 1
        return out

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股除权因子 → 内部契约(symbol/trade_date/ex_factor, 单事件比值非累积)。

        数据链: adjustment-factors 全量事件 dump → 清洗(时区 +8h / 滤全零 / 同日成分合并)
        → 前收盘价从本地日K dump 一次取齐(缺价标的回退单标的接口) → 交易所公式推导
        → 涨跌停自检剔除异常。
        未来已公告事件无前收盘, 留给滚动增量窗口(15 天)自然补上。
        """
        schema = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
        if not symbols or asset_type != "stock":
            return pl.DataFrame(schema=schema)
        try:
            events = self._load_adj_events(set(symbols), start_time, end_time)
        except FuyaoError as e:
            logger.warning("扶摇除权因子 dump 加载失败: %s", e)
            return pl.DataFrame(schema=schema)
        if events.is_empty():
            return pl.DataFrame(schema=schema)

        out_rows: list[dict] = []
        syms = sorted(events["symbol"].unique().to_list())
        # 前收盘价优先从本地日K dump 一次取齐(全市场配价从逐标的 ~13 分钟降为秒级),
        # 大 dump 不可用或个别标的缺价时回退单标的接口(节流保留)。
        bounds = events.group_by("symbol").agg(
            pl.col("ex_date").min().alias("first_ex"),
            pl.col("ex_date").max().alias("last_ex"),
        )
        closes_by_sym = self._closes_from_dumps(bounds)
        fallbacks = 0
        for i, sym in enumerate(syms):
            evs = events.filter(pl.col("symbol") == sym).sort("ex_date")
            first_ex: date = evs["ex_date"][0]
            last_ex: date = evs["ex_date"][-1]
            closes = (closes_by_sym or {}).get(sym)
            if not closes or min(closes) > first_ex:
                # dump 无该标的 / 覆盖不到首个事件前 → 单标的接口兜底
                closes = self._fetch_closes(
                    sym, first_ex - timedelta(days=_PREV_CLOSE_BACKDAYS), last_ex
                )
                time.sleep(_HIST_INTERVAL_S)
                fallbacks += 1
            if not closes:
                logger.warning("扶摇除权因子: %s 原始日K为空, 跳过其 %d 个事件", sym, evs.height)
                continue
            days = sorted(closes)
            for ev in evs.iter_rows(named=True):
                exd: date = ev["ex_date"]
                prev_days = [d for d in days if d < exd]
                if not prev_days:
                    continue  # 日K窗口未覆盖的远古事件(如 10 年分片边界之外)
                p = closes[prev_days[-1]]
                ref = _ref_price(p, ev["dividend"], ev["bonus"], ev["allot"], ev["allot_price"])
                if ref is None or ref <= 0:
                    logger.warning(
                        "扶摇除权因子: %s %s 参考价无法计算(P=%s D=%s S=%s AR=%s AP=%s), 跳过",
                        sym,
                        exd,
                        p,
                        ev["dividend"],
                        ev["bonus"],
                        ev["allot"],
                        ev["allot_price"],
                    )
                    continue
                factor = p / ref
                ex_days = [d for d in days if d >= exd]
                if ex_days:  # 涨跌停自检: 错误因子会令复权后除权日涨跌幅超出板限
                    ret = closes[ex_days[0]] / (p / factor) - 1.0
                    if abs(ret) > _price_limit(sym) + 0.02:
                        logger.warning(
                            "扶摇除权因子自检剔除 %s %s: 复权后除权日涨跌幅 %.1f%% 超出涨跌停",
                            sym,
                            exd,
                            ret * 100,
                        )
                        continue
                out_rows.append({"symbol": sym, "trade_date": exd, "ex_factor": factor})
            if on_chunk_done:
                on_chunk_done(i + 1, len(syms))
        if closes_by_sym is not None:
            logger.info(
                "扶摇除权因子: 本地 dump 配价 %d/%d 标的, 回退接口 %d 标的",
                len(closes_by_sym),
                len(syms),
                fallbacks,
            )
        if not out_rows:
            return pl.DataFrame(schema=schema)
        return (
            pl.DataFrame(out_rows, schema=schema)
            .unique(subset=["symbol", "trade_date"], keep="last")
            .sort(["symbol", "trade_date"])
        )

    def _load_adj_events(
        self,
        symset: set[str],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> pl.DataFrame:
        """事件 dump → 清洗 → 窗口过滤。返回 symbol/ex_date/dividend/bonus/allot/allot_price。

        清洗规则(对拍实证, 见 2026-08 验证记录):
        - ex_date_ms 为上海零点戳, +8h 转日期;
        - 全零事件行(疑似特殊事件, dump 未给成分)过滤;
        - 同日拆行(如分红/送转各一行)按成分合并后推导, 顺序不可反;
        - 配股但配股价缺失 → 无法推导, 过滤。
        """
        today = datetime.now().date()
        df = self._ensure_dump(_ADJ_DUMP_KIND, "adj_factors").rename({"thscode": "symbol"})
        df = (
            df.with_columns(
                pl.from_epoch(pl.col("ex_date_ms") + _SH_MS, time_unit="ms")
                .dt.date()
                .alias("ex_date"),
                pl.col("dividend_per_share").fill_null(0.0),
                pl.col("per_share_bonus").fill_null(0.0),
                pl.col("allotment_ratio").fill_null(0.0),
                pl.col("allotment_price").fill_null(0.0),
            )
            .filter(
                (pl.col("ex_date") <= today)  # 未来已公告事件无前收盘, 留给滚动增量
                & (
                    (pl.col("dividend_per_share") != 0)
                    | (pl.col("per_share_bonus") != 0)
                    | (pl.col("allotment_ratio") != 0)
                )
            )
            .group_by("symbol", "ex_date")
            .agg(
                pl.col("dividend_per_share").sum().alias("dividend"),
                pl.col("per_share_bonus").sum().alias("bonus"),
                pl.col("allotment_ratio").sum().alias("allot"),
                pl.col("allotment_price").max().alias("allot_price"),  # 同日拆行共享配股价
            )
            .filter(~((pl.col("allot") > 0) & (pl.col("allot_price") <= 0)))
            .filter(pl.col("symbol").is_in(sorted(symset)))
        )
        if start_time is not None:
            df = df.filter(pl.col("ex_date") >= start_time.date())
        if end_time is not None:
            df = df.filter(pl.col("ex_date") <= end_time.date())
        return df.select("symbol", "ex_date", "dividend", "bonus", "allot", "allot_price")

    # ---- financial ----
    # 字段映射: 扶摇原始字段 → 项目 canonical 列名(TickFlow 口径, 前端财务页与回测
    # FUNDAMENTAL_FACTORS 按此消费)。映射表之外的扶摇独有字段以原名透传为扩展列。
    _INCOME_FIELD_MAP = {
        "operating_income": "revenue",
        "operating_costs": "operating_cost",
        "sales_fee": "selling_expense",
        "manage_fee": "admin_expense",
        "research_and_development_expenses": "rd_expense",
        "operating_profit": "operating_profit",
        "interest_expenses": "financial_expense",  # 近似口径: 扶摇只给利息费用
        "profit_total": "total_profit",
        "income_tax_expense": "income_tax",
        "net_profit": "net_income",
        "parent_holder_net_profit": "net_income_attributable",
        "basic_eps": "basic_eps",
    }
    _BALANCE_FIELD_MAP = {
        "assets_total": "total_assets",
        "total_current_assets": "total_current_assets",
        "non_current_nets_total": "total_non_current_assets",
        "cash": "cash_and_equivalents",
        "accounts_receivable": "accounts_receivable",
        "total_debt": "total_liabilities",
        "holder_equity_total": "total_equity",
    }
    _CASHFLOW_FIELD_MAP = {
        "act_cash_flow_net": "net_operating_cash_flow",
        "invest_cash_flow_net": "net_investing_cash_flow",
        "financing_cash_flow_net": "net_financing_cash_flow",
        "pay_fixed_assets_etc_cash": "capex",
        "cash_equivalents_net_addition": "net_cash_change",
    }
    # 官方指标 index_id → canonical。归母净利同比近似 tickflow net_income_yoy;
    # 实测 index_id 与文档有出入(calculate_ 前缀等), 以实测为准。
    _METRICS_FIELD_MAP = {
        "index_weighted_avg_roe": "roe",
        "total_assets_net_ratio": "roa",
        "sale_gross_margin": "gross_margin",
        "sale_net_interest_ratio": "net_margin",
        "assets_debt_ratio": "debt_to_asset_ratio",
        "calculate_operating_income_yoy_growth_ratio": "revenue_yoy",
        "calculate_parent_holder_net_profit_yoy_growth_ratio": "net_income_yoy",
        "operating_cash_flow_net_divide_income": "operating_cash_to_revenue",
        "inventory_turnover_ratio": "inventory_turnover",
    }

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """拉取财务数据, 映射为 canonical 列(symbol/period_end/announce_date/指标)。

        - 三大报表: 单股单请求, latest_only 决定最近 1 期还是 8 期季报;
        - metrics: 指标接口为单股单期, 恒只拉最新一期(bps 由估值快照 pb_mrq 反推,
          eps_basic 顺带取自利润表); 历史各期建议切回 TickFlow 同步补齐 —
          报告期合并写入会让两源数据共存, 互不覆盖;
        - shares: 扶摇无股本接口, 恒返回空(已有存量靠合并写入保留)。
        """
        if table == "shares":
            logger.info("扶摇无股本接口, shares 表跳过 (已有数据保留)")
            return pl.DataFrame()
        if table in _STATEMENT_ENDPOINTS:
            field_map = {
                "income": self._INCOME_FIELD_MAP,
                "balance_sheet": self._BALANCE_FIELD_MAP,
                "cash_flow": self._CASHFLOW_FIELD_MAP,
            }[table]
            return self._financial_statements(table, field_map, symbols, latest_only)
        if table == "metrics":
            return self._financial_metrics(symbols)
        return pl.DataFrame()

    def _financial_statements(
        self,
        stmt: str,
        field_map: dict[str, str],
        symbols: list[str],
        latest_only: bool,
    ) -> pl.DataFrame:
        client = self._get_client()
        limit = 1 if latest_only else _FINANCIAL_HISTORY_PERIODS
        rows_out: list[dict] = []
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_HIST_INTERVAL_S)
            try:
                rows = client.financial_statements(stmt, sym, limit=limit)
            except FuyaoError as e:
                logger.warning("扶摇财务 %s %s 失败: %s", stmt, sym, e)
                continue
            for r in rows:
                row: dict = {
                    "symbol": sym,
                    "period_end": _iso_of_ms(r.get("period_end_ms")),
                    "announce_date": _iso_of_ms(r.get("report_date_ms")),
                }
                for src, dst in field_map.items():
                    row[dst] = _to_float(r.get(src))
                # 扶摇独有字段以原名透传为扩展列 (canonical 之外的增量信息)
                for src, value in r.items():
                    if src not in field_map and src not in row and isinstance(value, (int, float)):
                        row[src] = value
                rows_out.append(row)
        # 稀疏列(如仅银行有值的 financial_expense)前 100 行可能全空, 默认采样会
        # 推断成 Null 列, 后续数值 append 失败 → 全量扫描推断
        return pl.DataFrame(rows_out, infer_schema_length=None) if rows_out else pl.DataFrame()

    def _financial_metrics(self, symbols: list[str]) -> pl.DataFrame:
        client = self._get_client()
        # 指标接口按 report(yyyy-N) 单期查询 → 先用利润表 limit=1 反查每股最新披露期
        latest: dict[str, dict] = {}
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_HIST_INTERVAL_S)
            try:
                rows = client.financial_statements("income", sym, limit=1)
            except FuyaoError as e:
                logger.warning("扶摇财务 income %s 失败: %s", sym, e)
                continue
            if rows:
                latest[sym] = rows[0]
        if not latest:
            return pl.DataFrame()
        bps_by_sym = self._derive_bps(sorted(latest))
        rows_out: list[dict] = []
        for sym, r in latest.items():
            quarter = _report_quarter(r.get("fiscal_period"))
            report = f"{r.get('fiscal_year')}-{quarter}" if quarter else None
            row: dict = {
                "symbol": sym,
                "period_end": _iso_of_ms(r.get("period_end_ms")),
                "announce_date": _iso_of_ms(r.get("report_date_ms")),
                "eps_basic": _to_float(r.get("basic_eps")),
                "bps": bps_by_sym.get(sym),
            }
            if report:
                try:
                    abilities = client.financial_indicators(sym, report)
                except FuyaoError as e:
                    logger.warning("扶摇指标 %s %s 失败: %s", sym, report, e)
                    abilities = []
                for ability in abilities:
                    for ind in ability.get("indicators") or []:
                        index_id = ind.get("index_id")
                        if not index_id:
                            continue
                        value = _to_float(ind.get("value"))
                        if value is not None:
                            row[self._METRICS_FIELD_MAP.get(index_id, index_id)] = value
            rows_out.append(row)
        return pl.DataFrame(rows_out) if rows_out else pl.DataFrame()

    def trading_days(self) -> set:
        """近一年交易日集合 (供交易日探针)。失败抛 FuyaoError, 由探针兜为未知。"""
        rows = self._get_client().trading_days()
        return {
            d
            for d in (_date_of_ms(r.get("date_ms")) for r in rows)
            if d is not None
        }

    def dragon_tiger(self, board_type: str = "all", date: str | None = None) -> dict:
        """龙虎榜单榜 (复盘页卡片 + AI 复盘上下文)。返回原始 data 容器。

        非路由数据集 (tickflow 无对应能力), 不进 plugin.yaml datasets,
        由 services.dragon_tiger 统一做三榜聚合/缓存/交易日回退。
        """
        return self._get_client().dragon_tiger_list(board_type, date)

    def short_term_benchmark(self, date: str | None = None) -> dict:
        """短线风向标竞价基准 (复盘页卡片 + AI 复盘上下文)。返回原始 data 容器。

        非路由数据集 (tickflow 无对应能力), 不进 plugin.yaml datasets,
        由 services.auction_benchmark 统一做按日缓存/收益enrich/交易日回退。
        """
        return self._get_client().short_term_benchmark(date)

    def _derive_bps(self, symbols: list[str]) -> dict[str, float]:
        """估值快照 pb_mrq 与行情快照最新价同源同刻 → bps = price / pb_mrq。

        与财报口径 bps 可能差几个百分点(上游权益基准不完全透明), 用于补齐
        metrics.bps 使回测 pb_latest 因子可用。接口失败只影响 bps 列, 不致命。
        """
        client = self._get_client()
        pb: dict[str, float] = {}
        price: dict[str, float] = {}
        for i in range(0, len(symbols), _VALUATION_BATCH):
            if i:
                time.sleep(_HIST_INTERVAL_S)
            batch = symbols[i : i + _VALUATION_BATCH]
            for fetch, store in (
                (client.valuations_snapshot, pb),
                (client.price_snapshot_batch, price),
            ):
                time.sleep(_HIST_INTERVAL_S)
                try:
                    for r in fetch(batch):
                        value = _to_float(r.get("pb_mrq" if store is pb else "last_price"))
                        code = r.get("thscode")
                        if code and value is not None and value != 0:
                            store[code] = value
                except FuyaoError as e:
                    logger.warning("扶摇 bps 推导快照失败(%d 只): %s", len(batch), e)
        return {
            sym: price[sym] / pb[sym]
            for sym in symbols
            if sym in pb and pb[sym] and sym in price
        }

    def _fetch_closes(self, symbol: str, start_d: date, end_d: date) -> dict[date, float]:
        rows = self._historical_bars(symbol, start_d, end_d)
        out: dict[date, float] = {}
        for r in rows:
            d = _date_of_ms(r.get("date_ms"))
            c = _to_float(r.get("close_price"))
            if d is not None and c is not None:
                out[d] = c
        return out

    def _closes_from_dumps(
        self, bounds: pl.DataFrame
    ) -> dict[str, dict[date, float]] | None:
        """从本地日K dump 一次取齐全部事件标的的收盘价。

        每标的开窗 [first_ex-30d, last_ex](与单标的接口同窗): 10 年大 dump 为
        主体, 10d dump 叠加补末端新鲜度(除权日当天的自检需要 ex 日收盘)。
        返回 symbol → {date: close}; 大 dump 不可用/窗口早于其覆盖/读盘失败
        → None, 由调用方整轮回退单标的接口。
        """
        try:
            lo = bounds["first_ex"].min() - timedelta(days=_PREV_CLOSE_BACKDAYS)
            path = self._ensure_daily_big_dump(lo)
            if path is None:
                return None
            sources = [self._closes_scan(pl.scan_parquet(path))]
            ten = self._dump_memo.get(_DAILY10_DUMP_KIND)
            if ten is not None:
                sources.append(self._closes_scan(ten.lazy()))
            else:
                try:
                    p10 = self._ensure_dump_path(_DAILY10_DUMP_KIND, "daily_k_10d")
                except FuyaoError as e:
                    logger.info("扶摇 10d dump 不可用, 配价仅用 10 年 dump: %s", e)
                else:
                    sources.append(self._closes_scan(pl.scan_parquet(p10)))
            win = bounds.select(
                "symbol",
                (pl.col("first_ex") - pl.duration(days=_PREV_CLOSE_BACKDAYS)).alias("lo"),
                "last_ex",
            )
            # concat 顺序 = 叠加优先级: 同 (symbol, date) 时 10d dump(更新鲜)覆盖大 dump
            df = (
                pl.concat(sources, how="vertical_relaxed")
                .join(win.lazy(), on="symbol", how="inner")
                .filter(
                    (pl.col("date") >= pl.col("lo")) & (pl.col("date") <= pl.col("last_ex"))
                )
                .unique(subset=["symbol", "date"], keep="last", maintain_order=True)
                .collect()
            )
            return {
                f["symbol"][0]: dict(
                    zip(f["date"].to_list(), f["close"].to_list(), strict=True)
                )
                for f in df.partition_by("symbol")
            }
        except Exception as e:  # 缓存损坏等不致命: 回退逐标的接口
            logger.warning("扶摇除权因子本地配价失败, 回退单标的接口: %s", e)
            return None

    @staticmethod
    def _closes_scan(lf: pl.LazyFrame) -> pl.LazyFrame:
        """dump 行 → (symbol, date, close) lazy 投影; adjusted 列存在时锁 none。"""
        if "adjusted" in lf.collect_schema().names():
            lf = lf.filter(pl.col("adjusted") == "none")
        return lf.select(
            pl.col("thscode").alias("symbol"),
            pl.from_epoch(pl.col("date_ms").cast(pl.Int64) + _SH_MS, time_unit="ms")
            .dt.date()
            .alias("date"),
            pl.col("close_price").cast(pl.Float64).alias("close"),
        ).filter(pl.col("close").is_not_null())

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset in ("daily", "adj_factor"):
            syms = [s for s in (symbols or [])][:3] or ["000001.SZ"]
            try:
                if dataset == "daily":
                    df = self.get_daily(syms, datetime.now() - timedelta(days=30), datetime.now())
                else:
                    df = self.get_adj_factors(
                        syms, datetime.now() - timedelta(days=365), datetime.now()
                    )
            except FuyaoError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = df.head(5).to_dicts()
            for row in head:  # date/datetime → ISO 字符串, 保证 JSON 可序列化
                for k, v in list(row.items()):
                    if isinstance(v, (date, datetime)):
                        row[k] = v.isoformat()
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": df.height,
                "columns": df.columns,
                "preview": head,
            }
        if dataset == "financial":
            syms = [s for s in (symbols or [])][:1] or ["600519.SH"]
            try:
                df = self.get_financials("metrics", syms, latest_only=True)
            except FuyaoError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = df.head(5).to_dicts()
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": df.height,
                "columns": df.columns,
                "preview": head,
            }
        if dataset != "realtime":
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "error": f"扶摇插件未接入 {dataset} 数据集(自动回退 TickFlow)",
            }
        try:
            rows, count = self._get_client().snapshot_page(limit=5)
        except FuyaoError as e:
            return {"provider": self.name, "dataset": "realtime", "rows": 0, "error": str(e)}
        fetched_ms = int(time.time() * 1000)
        head = [r for r in (_map_snapshot_row(row, fetched_ms) for row in rows) if r][:5]
        return {
            "provider": self.name,
            "dataset": "realtime",
            "rows": count or len(head),
            "columns": list(head[0].keys()) if head else [],
            "preview": head,
        }
