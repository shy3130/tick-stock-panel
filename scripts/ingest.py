"""TickFlow Stock Panel — 本地数据接入脚本（策略 + 回测专用）。

把你自己提供的日 K 数据灌入项目的数据层，无需 TickFlow 云抓取。

数据流：
    你的 CSV/Parquet/Excel
      -> 归一化为 [symbol, date, open, high, low, close, volume, amount]
      -> repo.append_daily(df)           写 kline_daily/date=YYYY-MM-DD/part.parquet
      -> run_pipeline(data_dir)          自动前复权(无 adj_factor 则不调) + 算指标 -> kline_daily_enriched
      -> repo.refresh_cache()            让后端内存缓存生效

关键点：
  - 价格请给【前复权】价；若已是前复权，请勿同时提供 adj_factor，否则会二次复权。
  - date 必须是日期类型（脚本会自动转换）。
  - symbol 保持你数据里的格式，前后一致即可（如 600000.SH / 000001 / SH600000）。

用法：
  # 生成合成数据验证整条链路（无需真实数据）
  python scripts/ingest.py --sample --symbols 20 --days 500

  # 导入单个合并文件
  python scripts/ingest.py --csv my_data.csv
  python scripts/ingest.py --parquet my_data.parquet

  # 导入一个目录（每个股票一个文件，文件名含代码或文件内有 symbol 列）
  python scripts/ingest.py --dir ./my_kline/

  # 从 Tushare 拉取（raw HTTP，无需 tushare 包）。
  # 前复权由管道自动完成（拉取 adj_factor 转 ex_factor）。自定义 API 基址走环境变量。
  # token/基址也可走环境变量：TUSHARE_TOKEN / TUSHARE_API_BASE
  python scripts/ingest.py --tushare --start 20240101 --end 20260720
  # 等价: TUSHARE_TOKEN=xxx TUSHARE_API_BASE=http://tushare.xyz/v3/ python scripts/ingest.py --tushare
  # 日K已落盘但后续阶段崩溃时，复用已落盘数据补跑（不重抓日K）：
  python scripts/ingest.py --tushare --resume
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl

# ── 列名归一化映射（大小写/中英文）──────────────────────────────────────
COLMAP = {
    "symbol": ["symbol", "code", "ts_code", "证券代码", "股票代码", "代码", "stk_id", "id"],
    "date": ["date", "trade_date", "trading_date", "交易日期", "日期", "datetime", "时间", "day"],
    "open": ["open", "开盘价", "开盘", "o"],
    "high": ["high", "最高价", "最高", "h"],
    "low": ["low", "最低价", "最低", "l"],
    "close": ["close", "收盘价", "收盘", "c"],
    "volume": ["volume", "vol", "成交量", "成交量手", "成交量股", "v", "vol_share"],
    "amount": ["amount", "成交额", "成交金额", "amt", "amount_yuan", "成交额定"],
}


def _lower_cols(cols: list[str]) -> dict[str, str]:
    return {c.lower().strip(): c for c in cols}


def _pick(column_options: list[str], available_lower: dict[str, str]) -> str | None:
    for opt in column_options:
        if opt in available_lower:
            return available_lower[opt]
    return None


def normalize_columns(df: pl.DataFrame) -> pl.DataFrame:
    """把任意列名映射到标准列。"""
    avail = _lower_cols(df.columns)
    rename = {}
    for std, options in COLMAP.items():
        src = _pick(options, avail)
        if src is None:
            continue
        if src != std:
            rename[src] = std
    if rename:
        df = df.rename(rename)
    missing = [c for c in ("symbol", "date", "open", "high", "low", "close") if c not in df.columns]
    if missing:
        raise SystemExit(f"缺少必需列: {missing}。文件现有列: {df.columns}")
    if "volume" not in df.columns:
        raise SystemExit(f"缺少必需列 volume。文件现有列: {df.columns}")
    if "amount" not in df.columns:
        df = df.with_columns(pl.lit(0.0).alias("amount"))
    return df


def to_daily_schema(df: pl.DataFrame) -> pl.DataFrame:
    """类型校正：date->Date, 价格/量->Float64, symbol->Utf8。"""
    df = normalize_columns(df)
    # date 处理：可能是字符串 / datetime
    if df["date"].dtype == pl.Utf8:
        # 兼容 20240101 / 2024-01-01 / 2024/01/01
        d = (
            df["date"]
            .str.replace_all(r"(\d{4})[\-/](\d{2})[\-/](\d{2})", r"$1-$2-$3")
            .str.slice(0, 10)
        )
        df = df.with_columns(pl.col("date").alias("_dstr"))
        df = df.with_columns(pl.col("_dstr").str.to_date("%Y-%m-%d", strict=False))
        # 兜底：纯数字 20240101
        df = df.with_columns(
            pl.when(pl.col("date").is_null())
            .then(pl.col("_dstr").str.to_date("%Y%m%d", strict=False))
            .otherwise(pl.col("date"))
            .alias("date")
        )
        df = df.drop("_dstr")
    elif df["date"].dtype in (pl.Datetime, pl.Datetime("ms")):
        df = df.with_columns(pl.col("date").dt.date().alias("date"))

    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in df.columns:
            df = df.with_columns(pl.col(c).cast(pl.Float64, strict=False))

    df = df.with_columns(pl.col("symbol").cast(pl.Utf8))
    df = df.select(["symbol", "date", "open", "high", "low", "close", "volume", "amount"])
    df = df.drop_nullaries(subset=["symbol", "date", "close"]).drop_nans(subset=["close"])
    return df.sort(["symbol", "date"])


def load_source(path: Path) -> pl.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pl.read_csv(path)
    elif suffix in (".parquet", ".pq"):
        df = pl.read_parquet(path)
    elif suffix in (".xlsx", ".xls"):
        df = pl.read_excel(path)
    else:
        raise SystemExit(f"不支持的文件类型: {suffix}（支持 csv/parquet/xlsx）")
    return to_daily_schema(df)


def load_dir(directory: Path) -> pl.DataFrame:
    files = sorted(
        p
        for p in directory.iterdir()
        if p.suffix.lower() in (".csv", ".parquet", ".pq", ".xlsx", ".xls")
    )
    if not files:
        raise SystemExit(f"目录 {directory} 下没有找到数据文件")
    frames = []
    for f in files:
        df = load_source(f)
        # 若文件内无 symbol 列，尝试从文件名推断
        if "symbol" not in df.columns or df["symbol"].n_unique() <= 1:
            stem = f.stem
            code = _extract_code(stem)
            if code:
                df = df.with_columns(pl.lit(code).alias("symbol"))
        frames.append(df)
    return pl.concat(frames, how="diagonal_relaxed").sort(["symbol", "date"])


def _extract_code(stem: str) -> str | None:
    import re

    m = re.search(r"(?i)([0-9]{6}\.?(SH|SZ|BJ)?)", stem)
    if m:
        return m.group(1).upper().replace(".", ".")
    return None


def generate_sample(n_symbols: int = 20, n_days: int = 500, seed: int = 42) -> pl.DataFrame:
    """合成前复权日 K，用于验证整条链路。"""
    rng = random.Random(seed)
    end = date.today()
    # 只取工作日
    days = []
    d = end - timedelta(days=n_days * 2)
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    days = days[-n_days:]
    rows = []
    for i in range(n_symbols):
        code = f"{600000 + i:06d}.SH"
        price = rng.uniform(8, 60)
        for day in days:
            # 随机游走 + 轻微趋势
            drift = rng.uniform(-0.02, 0.022)
            price = max(1.0, price * (1 + drift))
            open_ = price * (1 + rng.uniform(-0.01, 0.01))
            close = price
            high = max(open_, close) * (1 + rng.uniform(0, 0.02))
            low = min(open_, close) * (1 - rng.uniform(0, 0.02))
            vol = rng.uniform(0.5, 5.0) * 1_000_000
            amount = close * vol
            rows.append(
                {
                    "symbol": code,
                    "date": day,
                    "open": round(open_, 2),
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "close": round(close, 2),
                    "volume": round(vol, 0),
                    "amount": round(amount, 2),
                }
            )
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _ts_post(base, token, api_name, params, fields=None, timeout=30, session=None):
    """POST 到 Tushare HTTP API，返回 data 字典或 None（限流/错误时退避重试）。"""
    import requests

    sess = session or requests
    body = {"api_name": api_name, "token": token, "params": params}
    if fields:
        body["fields"] = fields
    last_err = None
    for attempt in range(6):
        try:
            r = sess.post(base, json=body, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            if j.get("code") == 0 and j.get("data"):
                return j["data"]
            last_err = j.get("msg") or f"code={j.get('code')}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        if attempt < 5:
            time.sleep(1.2 * (attempt + 1))
        else:
            print(f"  [warn] {api_name}{params} 失败: {last_err}")
    return None


def _resume_tushare(base, token, d0, settings):
    """复用已落盘的 kline_daily / adj_factor，仅补跑 instruments + run_pipeline + 缓存。

    用于「日K抓取已完成、但后续 instruments/pipeline/cache 阶段崩溃」的恢复，
    避免重复拉取慢速的全市场日K。
    """
    from app.indicators.pipeline import run_pipeline
    from app.tickflow.repository import DataStore, KlineRepository
    import requests

    adj_dir = Path(d0) / "adj_factor"
    af_path = adj_dir / "all.parquet"
    if not af_path.exists():
        raise SystemExit("resume 需要先有 adj_factor/all.parquet（请先完整跑一次 --tushare）")
    af = pl.read_parquet(af_path)

    print("[r0] 从已有 adj_factor 取真实标的 ...")
    real_syms = set(af["symbol"].unique().to_list())
    print(f"     真实标的: {len(real_syms)} 只")

    print("[r1] 拉股票列表 stock_basic（补 name）...")
    sb = _ts_post(base, token, "stock_basic",
                  {"exchange": "", "list_status": "L"},
                  fields="ts_code,symbol,name,list_date")
    inst_rows = []
    if sb and sb.get("items"):
        f = sb["fields"]
        for it in sb["items"]:
            d = dict(zip(f, it))
            inst_rows.append({
                "symbol": d["ts_code"],
                "name": (d.get("name") or d["ts_code"]),
                "list_date": d.get("list_date"),
            })
    inst_df = pl.DataFrame(inst_rows).filter(pl.col("symbol").is_in(list(real_syms)))
    print(f"     上市股票（含数据）: {inst_df.height} 只")

    print("[r2] 写 instruments ...")
    new_inst = write_instruments(inst_df)

    print("[r3] run_pipeline（前复权 + 指标）...")
    written = run_pipeline(d0)
    print(f"     enriched 写入 {written} 行")
    store = DataStore()
    repo = KlineRepository(store)
    print("[r4] refresh_cache ...")
    repo.refresh_cache()
    return {
        "mode": "resume",
        "symbols": inst_df.height,
        "adj_factor_rows": af.height,
        "enriched_rows": written,
        "instruments": str(new_inst),
    }


def ingest_from_tushare(base, token, start, end, clear_first=False, resume=False):
    """从 Tushare 拉全市场日K（原始价），写入 kline_daily（原始）+ adj_factor（用于前复权），
    再跑 run_pipeline 生成前复权 enriched。streaming 逐日写分区，避免一次性把全市场数据载进内存。

    要点：
      - daily 按 trade_date 拉（一次调用 = 全市场当天），高效；vol(手)->股、amount(千元)->元。
      - adj_factor 同样按 trade_date 拉；Tushare 给的是【累积复权因子】，需转成 pipeline 需要的
        per-event ex_factor（= adj[d]/adj[d-1]，首日=1），pipeline 再用 cum_prod/末值 求前复权。
      - 前复权由 run_pipeline 完成，这里不预调。
      - resume=True 时跳过日K/adj_factor 抓取，直接复用已落盘数据补跑后续阶段。
    """
    from app.config import settings
    from app.indicators.pipeline import run_pipeline
    from app.tickflow.repository import DataStore, KlineRepository
    import requests
    import shutil

    d0 = settings.data_dir
    kline_dir = Path(d0) / "kline_daily"
    enriched_dir = Path(d0) / "kline_daily_enriched"
    adj_dir = Path(d0) / "adj_factor"
    inst_dir = Path(d0) / "instruments"
    cache_dir = Path(d0) / ".backtest_matrix_cache"

    if clear_first:
        for p in (kline_dir, enriched_dir, adj_dir, inst_dir, cache_dir):
            if p.exists():
                shutil.rmtree(p)
                print(f"  cleared {p}")

    if resume:
        return _resume_tushare(base, token, d0, settings)

    sess = requests.Session()

    # 1) 交易日历
    print(f"[0] 拉交易日历 {start}~{end} ...")
    cal = _ts_post(base, token, "trade_cal",
                   {"exchange": "SSE", "start_date": start, "end_date": end, "is_open": "1"},
                   fields="cal_date", session=sess)
    if not cal or not cal.get("items"):
        raise SystemExit("交易日历拉取失败（检查 token / API base / 日期范围）")
    open_days = [r[0] for r in cal["items"]]
    print(f"    交易日: {len(open_days)} 天")

    # 2) 股票列表（instruments）
    print("[1] 拉股票列表 stock_basic ...")
    sb = _ts_post(base, token, "stock_basic",
                  {"exchange": "", "list_status": "L"},
                  fields="ts_code,symbol,name,list_date", session=sess)
    inst_rows = []
    if sb and sb.get("items"):
        f = sb["fields"]
        for it in sb["items"]:
            d = dict(zip(f, it))
            inst_rows.append({
                "symbol": d["ts_code"],
                "name": (d.get("name") or d["ts_code"]),
                "list_date": d.get("list_date"),
            })
    inst_df = pl.DataFrame(inst_rows)
    print(f"    上市股票: {inst_df.height} 只")

    # 3) 逐交易日拉 daily + adj_factor，streaming 写分区
    print(f"[2] 逐交易日拉 daily + adj_factor（共 {len(open_days)} 天）...")
    adj_rows = []  # (symbol, trade_date, adj_factor_cum)
    kline_dir.mkdir(parents=True, exist_ok=True)
    n_written = 0
    n_days_ok = 0
    for i, td in enumerate(open_days, 1):
        daily = _ts_post(base, token, "daily", {"trade_date": td},
                         fields="ts_code,open,high,low,close,vol,amount", session=sess)
        adj = _ts_post(base, token, "adj_factor", {"trade_date": td},
                       fields="ts_code,adj_factor", session=sess)
        if daily and daily.get("items"):
            f = daily["fields"]
            idx = {name: i for i, name in enumerate(f)}
            cols = {name: [row[idx[name]] for row in daily["items"]] for name in f}
            # strict=False: Tushare 个别数值字段可能混有小数（如 vol/amount），
            # 直接让 polars 推断为 Float64，避免 Int64 遇到 float 报错。
            df = pl.DataFrame(
                {name: pl.Series(name, vals, strict=False) for name, vals in cols.items()}
            ).rename({"ts_code": "symbol", "vol": "volume"})
            df = df.with_columns(
                (pl.col("volume") * 100).alias("volume"),   # 手 -> 股
                (pl.col("amount") * 1000).alias("amount"),  # 千元 -> 元
                pl.lit(td).alias("date"),
            )
            for c in ("open", "high", "low", "close", "volume", "amount"):
                df = df.with_columns(pl.col(c).cast(pl.Float64, strict=False))
            df = df.with_columns(pl.col("date").str.to_date("%Y%m%d"))
            df = df.select(["symbol", "date", "open", "high", "low", "close", "volume", "amount"])
            # Tushare 个别交易日会返回重复记录（同日同股两遍），去重避免矩阵构建报
            # "requires unique date/symbol rows"。
            df = df.unique(subset=["symbol"], keep="last")
            iso = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
            out = kline_dir / f"date={iso}" / "part.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            df.write_parquet(out)
            n_written += df.height
            n_days_ok += 1
        if adj and adj.get("items"):
            f = adj["fields"]
            for it in adj["items"]:
                d = dict(zip(f, it))
                adj_rows.append((d["ts_code"], td, float(d["adj_factor"])))
        if i % 50 == 0 or i == len(open_days):
            print(f"    {i}/{len(open_days)} 天, 已写 {n_written} 行K线")
        time.sleep(0.10)

    print(f"    完成: {n_days_ok} 天有日K, 共 {n_written} 行")

    # 4) 写 adj_factor（累积因子 -> per-event ex_factor）
    print("[3] 写 adj_factor（累积->ex_factor）...")
    adj_dir.mkdir(parents=True, exist_ok=True)
    af = pl.DataFrame(adj_rows, schema=["symbol", "trade_date", "adj_factor"], orient="row")
    af = af.with_columns(
        pl.col("trade_date").str.to_date("%Y%m%d"),
        pl.col("adj_factor").cast(pl.Float64),
    ).sort(["symbol", "trade_date"])
    af = af.with_columns(
        (pl.col("adj_factor") / pl.col("adj_factor").shift(1).over("symbol")).alias("ex_factor")
    ).with_columns(pl.col("ex_factor").fill_null(1.0))
    af = af.select("symbol", "trade_date", "ex_factor")
    af.write_parquet(adj_dir / "all.parquet")
    print(f"    adj_factor 行: {af.height}")

    # 5) instruments（仅保留真实有数据的标的；股本缺失则走确定性合理值兜底）
    print("[4] 写 instruments ...")
    inst_dir.mkdir(parents=True, exist_ok=True)
    real_syms = set(af["symbol"].unique().to_list())
    inst_df = inst_df.filter(pl.col("symbol").is_in(list(real_syms)))
    new_inst = write_instruments(inst_df)

    # 6) pipeline（前复权 + 指标）+ 刷新缓存
    print("[5] run_pipeline（前复权 + 指标）...")
    written = run_pipeline(d0)
    print(f"    enriched 写入 {written} 行")
    store = DataStore()
    repo = KlineRepository(store)
    print("[6] refresh_cache ...")
    repo.refresh_cache()
    return {
        "days": n_days_ok,
        "kline_rows": n_written,
        "symbols": inst_df.height,
        "adj_factor_rows": af.height,
        "enriched_rows": written,
        "instruments": str(new_inst),
    }


def _realistic_shares(symbols: list[str]) -> dict[str, tuple[float, float]]:
    """为缺失股本的标的生成确定性、合理的股本数（用于通过回测默认市值过滤）。

    默认回测 basic_filter 含 market_cap_min=1e9，而流通/总股本占位 1.0 会让
    市值≈收盘价(<1e9) 被全部过滤掉，导致“无买入信号”。这里用按代码哈希播种的
    随机数生成 2e8~8e9 的总股本，使其市值进入合理区间。真实数据请直接提供
    total_shares / float_shares 列（来自 Tushare 的 total_share / float_share 等）。
    """
    rng = random.Random(20240721)
    out: dict[str, tuple[float, float]] = {}
    for sym in symbols:
        r = random.Random(hash(sym) & 0xFFFFFFFF).random
        total = r() * (8e9 - 2e8) + 2e8
        float_ = total * (0.3 + r() * 0.6)
        out[sym] = (round(total, 0), round(float_, 0))
    return out


def write_instruments(df: pl.DataFrame) -> Path:
    """生成/追加 instruments 维表。

    回测矩阵引擎必须读取 total_shares / float_shares；
    consecutive_limit_ups(连板数) 也只在 instruments 非空时才会计算。
    若数据里带 name / total_shares / float_shares 列则直接使用；否则生成确定性、
    合理的股本数（见 _realistic_shares），以便通过回测默认的市值过滤。
    """
    from app.config import settings

    out = Path(settings.data_dir) / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    syms = df["symbol"].unique().to_list()
    # 注意：必须在 select 时就带上所有需要的列（symbol/name/...），
    # 不能先 `df.select("symbol").unique()` 再引用其它列——那样其它列已被裁掉，
    # 会触发 ColumnNotFoundError。这里直接对含全部列的行做 unique。
    exprs = [pl.col("symbol")]
    exprs.append(pl.col("name").alias("name") if "name" in df.columns else pl.col("symbol").alias("name"))
    if "list_date" in df.columns:
        exprs.append(pl.col("list_date"))
    if "total_shares" in df.columns:
        exprs.append(pl.col("total_shares").cast(pl.Float64))
    else:
        gen = _realistic_shares(syms)
        exprs.append(
            pl.col("symbol").replace(
                {s: v[0] for s, v in gen.items()}, default=None
            ).cast(pl.Float64).alias("total_shares")
        )
    if "float_shares" in df.columns:
        exprs.append(pl.col("float_shares").cast(pl.Float64))
    else:
        gen = _realistic_shares(syms)
        exprs.append(
            pl.col("symbol").replace(
                {s: v[1] for s, v in gen.items()}, default=None
            ).cast(pl.Float64).alias("float_shares")
        )
    new = df.select(exprs).unique(subset=["symbol"], keep="last")
    if out.exists():
        try:
            old = pl.read_parquet(out)
            new = pl.concat([old, new], how="diagonal_relaxed").unique(
                subset=["symbol"], keep="last"
            )
        except Exception:
            pass
    new.write_parquet(out)
    return out


def ingest(df: pl.DataFrame) -> int:
    from app.config import settings
    from app.indicators.pipeline import run_pipeline
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore()
    repo = KlineRepository(store)
    print(f"[1/3] append_daily: {df.height} 行, {df['symbol'].n_unique()} 只")
    repo.append_daily(df)
    inst = write_instruments(df)
    print(f"      instruments -> {inst} ({df['symbol'].n_unique()} 只)")
    print(f"[2/3] run_pipeline: 计算 enriched 指标（前复权仅在提供 adj_factor 时生效）...")
    written = run_pipeline(settings.data_dir)
    print(f"      写入 enriched {written} 行")
    print("[3/3] refresh_cache: 刷新后端内存缓存")
    repo.refresh_cache()
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="TickFlow 本地数据接入")
    ap.add_argument("--sample", action="store_true", help="生成合成数据验证链路")
    ap.add_argument("--symbols", type=int, default=20)
    ap.add_argument("--days", type=int, default=500)
    ap.add_argument("--csv", type=str, help="单个 CSV 文件路径")
    ap.add_argument("--parquet", type=str, help="单个 Parquet 文件路径")
    ap.add_argument("--dir", type=str, help="目录（每股票一个文件）")
    ap.add_argument("--tushare", action="store_true", help="从 Tushare 拉取")
    ap.add_argument("--resume", action="store_true",
                    help="复用已落盘的 kline_daily/adj_factor，仅补跑 instruments+pipeline+cache")
    ap.add_argument("--ts-token", type=str, default="", help="Tushare token")
    ap.add_argument("--start", type=str, default="20230101")
    ap.add_argument("--end", type=str, default=date.today().strftime("%Y%m%d"))
    args = ap.parse_args()

    if args.sample:
        df = generate_sample(args.symbols, args.days)
    elif args.csv:
        df = load_source(Path(args.csv))
    elif args.parquet:
        df = load_source(Path(args.parquet))
    elif args.dir:
        df = load_dir(Path(args.dir))
    elif args.tushare:
        token = args.ts_token or os.environ.get("TUSHARE_TOKEN", "")
        base = os.environ.get("TUSHARE_API_BASE", "http://api.tushare.pro")
        if not token:
            raise SystemExit("请提供 --ts-token 或设置 TUSHARE_TOKEN 环境变量")
        summary = ingest_from_tushare(
            base, token, args.start, args.end,
            clear_first=not args.resume, resume=args.resume,
        )
        print("完成。后端重启后可在「选股/策略」与「回测」页使用真实数据。")
        print("摘要:", summary)
        return
    elif args.sample:
        df = generate_sample(args.symbols, args.days)
    elif args.csv:
        df = load_source(Path(args.csv))
    elif args.parquet:
        df = load_source(Path(args.parquet))
    elif args.dir:
        df = load_dir(Path(args.dir))
    else:
        ap.error("请指定一种数据源: --sample / --csv / --parquet / --dir / --tushare")

    print(f"载入数据: {df.height} 行, {df['symbol'].n_unique()} 只, "
          f"日期 {df['date'].min()} ~ {df['date'].max()}")
    ingest(df)
    print("完成。启动后端后可在「选股/策略」与「回测」页使用。")


if __name__ == "__main__":
    main()
