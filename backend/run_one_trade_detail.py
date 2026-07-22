"""
取 pullback_to_support 在 2026-03-24~06-24(结构牛) 的成交单，挑一天(默认中间那笔)，
从该票 enriched 行情补全买入上下文(MA20/MA60/量比/动量)，输出分析细节 JSON。
"""
import json
from datetime import date as _date

import polars as pl

from app.config import settings
from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task


def run_backtest():
    cfg = StrategyBacktestConfig(
        strategy_id="pullback_to_support", symbols=None,
        start=_date(2026, 3, 24), end=_date(2026, 6, 24),
        params=None, overrides=None, matching="open_t+1",
        entry_fill=None, exit_fill=None, fees_pct=0.0002,
        commission_pct=None, stamp_tax_pct=None, slippage_bps=5.0,
        max_positions=5, max_exposure_pct=1.0,
        initial_capital=1_000_000.0, position_sizing="score_weight",
        mode="position", holding_days=5, asset_type="stock", minute_fill=False,
        regime_filter=None,
    )
    res = run_worker_task(make_worker_task("backtest", settings.data_dir, cfg))
    return res.get("trades") or []


def load_kline(symbol):
    files = __import__("glob").glob(
        str(settings.data_dir / "kline_daily_enriched" / "**" / f"{symbol}.parquet"),
        recursive=True)
    if not files:
        # 退而求其次看 kline_daily
        files = __import__("glob").glob(
            str(settings.data_dir / "kline_daily" / "**" / f"{symbol}.parquet"),
            recursive=True)
    if not files:
        return None
    df = pl.read_parquet(files[0]).sort("date")
    has_vol = "volume" in df.columns
    if has_vol:
        df = df.with_columns(
            (pl.col("close").rolling_mean(20)).alias("ma20"),
            (pl.col("close").rolling_mean(60)).alias("ma60"),
            (pl.col("volume") / pl.col("volume").rolling_mean(5)).alias("vol_ratio_5d"),
            (pl.col("close") / pl.col("close").shift(20) - 1).alias("momentum_20d"),
            (pl.col("close") / pl.col("close").shift(60) - 1).alias("momentum_60d"),
        )
    else:
        df = df.with_columns(
            (pl.col("close").rolling_mean(20)).alias("ma20"),
            (pl.col("close").rolling_mean(60)).alias("ma60"),
            (pl.col("close") / pl.col("close").shift(20) - 1).alias("momentum_20d"),
            (pl.col("close") / pl.col("close").shift(60) - 1).alias("momentum_60d"),
        )
    return df


def main():
    trades = run_backtest()
    print(f"[detail] 成交单数: {len(trades)}", flush=True)
    # 挑中间那笔(随机一天的代表)；优先挑有正收益的
    picks = [t for t in trades if (t.get("pnl_pct") or 0) > 0]
    chosen = (picks or trades)[len(picks or trades) // 2]
    sym = chosen["symbol"]
    ed = chosen["entry_date"]
    ep = chosen["entry_price"]
    print(f"[detail] 选中: {sym} 入场 {ed} 价 {ep} pnl={chosen.get('pnl_pct')}", flush=True)

    df = load_kline(sym)
    detail = dict(chosen)
    if df is not None:
        try:
            edate = _date.fromisoformat(str(ed)[:10])
        except Exception:
            edate = None
        row = df.filter(pl.col("date") == edate) if edate else None
        if row is not None and row.height:
            r = row.row(0, named=True)
            close = r.get("close")
            ma20 = r.get("ma20")
            ma60 = r.get("ma60")
            vr = r.get("vol_ratio_5d")
            m20 = r.get("momentum_20d")
            m60 = r.get("momentum_60d")
            detail["ctx"] = {
                "close": close, "ma20": ma20, "ma60": ma60,
                "vol_ratio_5d": vr, "momentum_20d": m20, "momentum_60d": m60,
                "near_ma20_pct": (close / ma20 - 1) if (close and ma20) else None,
                "above_ma60": bool(close > ma60) if (close and ma60) else None,
                "vol_shrink": bool(vr < 0.8) if vr is not None else None,
                "mom20_pos": bool(m20 > 0) if m20 is not None else None,
            }
            # 止损价 -5%
            detail["stop_loss_price"] = round(ep * 0.95, 3) if ep else None
    out = settings.data_dir.parent / "strategy_one_trade_detail.json"
    out.write_text(json.dumps(detail, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[detail] 写出 {out}", flush=True)


if __name__ == "__main__":
    main()
