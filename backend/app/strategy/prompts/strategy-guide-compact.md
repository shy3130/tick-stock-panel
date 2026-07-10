# AI 策略生成精简指南

只生成一个完整 Python 策略文件，直接输出代码，不要输出 Markdown 解释。

## 必须遵守

1. 只 `import polars as pl`，禁止 import 其他模块。
2. AI 策略只属于 `data/strategies/ai/`，`META.id` 使用用户给定的 `ai_` ID。
3. 不要读写文件，不要使用 `open/exec/eval/compile/__import__/globals/locals/vars/dir/getattr/setattr/delattr/type/input`。
4. `META.params` 只放用户可能调整的阈值；公式常数和固定窗口边界不必参数化。
5. `META.scoring` 权重总和必须为 1.0。
6. `ENTRY_SIGNALS` / `EXIT_SIGNALS` 只选和策略逻辑直接相关的信号，不要凑数。
7. `RULES` 用中文逐条列出核心逻辑，至少 3 条。
8. 优先 Polars 表达式、`with_columns`、`over("symbol")`、`group_by`、`join`、`filter`，避免逐行循环。

## 文件结构

```python
"""策略简短描述"""
import polars as pl

META = {
    "id": "ai_xxxxxxxxxxxx",
    "name": "策略中文名",
    "description": "一句话说明策略逻辑",
    "tags": ["标签"],
    "basic_filter": {
        "price_min": 3,
        "price_max": 200,
        "market_cap_min": 10e8,
        "amount_min": 0.5e8,
        "exclude_st": True,
        "exclude_new_days": 30,
    },
    "params": [],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20
ALERTS = []

RULES = """
1. 规则一
2. 规则二
3. 规则三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
```

## 何时使用 filter_history

普通 `filter()` 只判断当日数据。规则涉及以下场景时必须使用 `filter_history(df, params) -> pl.DataFrame`：

- 最近 N 天内出现过某事件。
- 涨停后的第 X 天、上次涨停价、前高、前低。
- 连续 N 天阴跌/阳线等时序逻辑。
- 任何需要多日数据才能判断的条件。

历史窗口策略要声明：

```python
LOOKBACK_DAYS = 8

def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    if df.is_empty() or "date" not in df.columns:
        return df
    hist = df.sort(["symbol", "date"]).with_columns([
        pl.col("close").shift(1).over("symbol").alias("_prev_close"),
    ])
    return hist.filter(pl.col("close") > pl.col("_prev_close"))
```

`filter_history()` 必须返回所有匹配行，不要只过滤最新日期；回测需要全区间命中。

## 常用字段

通用：`symbol`, `date`, `name`

价格：`open`, `high`, `low`, `close`, `raw_close`, `raw_high`, `raw_low`, `prev_close`, `change_pct`, `change_amount`, `amount`, `amplitude`

均线：`ma5`, `ma10`, `ma20`, `ma30`, `ma60`, `ema5`, `ema10`, `ema20`, `ema30`, `ema60`

技术指标：`macd_dif`, `macd_dea`, `macd_hist`, `boll_upper`, `boll_lower`, `kdj_k`, `kdj_d`, `kdj_j`, `rsi_6`, `rsi_14`, `rsi_24`, `atr_14`

量能：`volume`, `vol_ma5`, `vol_ma10`, `vol_ratio_5d`, `turnover_rate`

动量与波动：`momentum_5d`, `momentum_10d`, `momentum_20d`, `momentum_30d`, `momentum_60d`, `annual_vol_20d`, `high_60d`, `low_60d`

涨跌停：`consecutive_limit_ups`, `consecutive_limit_downs`

市值相关：`total_shares`, `float_shares`，可用 `close * total_shares` 估算总市值。

## 常用信号列

信号列是布尔值，使用时加 `.fill_null(False)`。

- `signal_ma_golden_5_20`: MA5 上穿 MA20
- `signal_ma_dead_5_20`: MA5 下穿 MA20
- `signal_ma_golden_20_60`: MA20 上穿 MA60
- `signal_macd_golden`: MACD 金叉
- `signal_macd_dead`: MACD 死叉
- `signal_ma20_breakout`: 突破 MA20
- `signal_ma20_breakdown`: 跌破 MA20
- `signal_n_day_high`: 60 日新高
- `signal_n_day_low`: 60 日新低
- `signal_boll_breakout_upper`: 突破布林上轨
- `signal_boll_breakdown_lower`: 跌破布林下轨
- `signal_volume_surge`: 放量
- `signal_limit_up`: 涨停
- `signal_limit_down`: 跌停
- `signal_limit_down_recovery`: 跌停翘板
- `signal_broken_limit_up`: 炸板

涨跌停策略优先使用稳定列 `consecutive_limit_ups >= 1`。

## 不可直接引用的数据

以下数据不在 enriched DataFrame 中，策略代码不能直接引用：财务数据、扩展数据、概念/行业/人气排名/资金流向、盘中分时价、五档盘口。
