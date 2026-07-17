# 两步创建示例：强势反包策略

本文演示从零创建一个自定义策略的完整流程。

---

## Step 1：填写规则

用户在创建对话框填写：

| 字段 | 填写内容 |
|------|---------|
| 名称 | **强势反包** |
| 描述 | 筛选前日阴线下跌、今日放量阳线反包的短线强势股 |
| 方向 | 做多 |
| 规则 | 前一交易日为明显阴线且跌幅不低于2%，今日阳线收盘反包前一日实体，收盘价接近或高于前一日高点，成交量较前一日放大1.2倍以上，当前 close > ma5 或 close > ma10；使用 filter_history，并优先用 Polars shift/with_columns/filter 实现。 |

点击「AI 生成」，AI 返回完整策略代码（含参数、信号、评分、告警）：

```python
"""强势反包 — 前日阴线下跌 + 今日放量阳线反包"""
import polars as pl

META = {
    "id": "strong_reversal",
    "name": "强势反包",
    "description": "筛选前日阴线下跌、今日放量阳线反包的短线强势股",
    "tags": ["反包", "短线", "放量"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 3,
        "price_max": 200,
        "market_cap_min": 10e8,
        "amount_min": 0.5e8,
        "exclude_st": True,
        "exclude_new_days": 30,
    },
    "params": [
        {
            "id": "prev_down_pct",
            "label": "前日最大跌幅",
            "type": "float",
            "default": -0.02,
            "min": -0.10,
            "max": -0.005,
            "step": 0.005,
        },
        {
            "id": "volume_ratio",
            "label": "成交量放大倍数",
            "type": "float",
            "default": 1.2,
            "min": 1.0,
            "max": 5.0,
            "step": 0.1,
        },
        {
            "id": "reversal_tolerance",
            "label": "反包容忍误差",
            "type": "float",
            "default": 0.005,
            "min": 0.0,
            "max": 0.03,
            "step": 0.005,
        },
    ],
    "scoring": {
        "change_pct": 0.4,
        "vol_ratio_5d": 0.3,
        "momentum_5d": 0.3,
    },
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "python_history_legacy"
LOOKBACK_DAYS = 2

ENTRY_SIGNALS = ["signal_broken_board_recovery"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 10
ALERTS = [
    {"field": "signal_broken_board_recovery", "message": "反包信号"},
]

RULES = """
1. 前一交易日为阴线，且跌幅不小于设定阈值
2. 今日为阳线，收盘价收复前一日开盘价并接近或突破前一日高点
3. 今日成交量较前一日明显放大，且收盘价站上 MA5 或 MA10
"""


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    if df.is_empty() or "date" not in df.columns:
        return df

    down_pct = float(params.get("prev_down_pct", -0.02))
    vol_ratio = float(params.get("volume_ratio", 1.2))
    tolerance = float(params.get("reversal_tolerance", 0.005))
    latest = df["date"].max()
    hist = (
        df.sort(["symbol", "date"])
        .with_columns([
            pl.col("open").shift(1).over("symbol").alias("_prev_open"),
            pl.col("high").shift(1).over("symbol").alias("_prev_high"),
            pl.col("close").shift(1).over("symbol").alias("_prev_close"),
            pl.col("volume").shift(1).over("symbol").alias("_prev_volume"),
            pl.col("change_pct").shift(1).over("symbol").alias("_prev_change_pct"),
        ])
    )
    return hist.filter(pl.col("date") == latest).filter(
        (pl.col("_prev_close") < pl.col("_prev_open"))
        & (pl.col("_prev_change_pct") <= down_pct)
        & (pl.col("close") > pl.col("open"))
        & (pl.col("close") > pl.col("_prev_open"))
        & (pl.col("close") >= pl.col("_prev_high") * (1 - tolerance))
        & (pl.col("volume") >= pl.col("_prev_volume") * vol_ratio)
        & ((pl.col("close") > pl.col("ma5")) | (pl.col("close") > pl.col("ma10")))
    )
```

---

## Step 2：预览 + 指令修改

进入第二步，显示完整的策略预览和指令输入框。

用户如果觉得反包条件太严格，可以输入「把前日跌幅放宽到 -1.5%，反包前高允许 1% 误差」→ 点 AI 修改。AI 更新 `params` 默认值和 `filter_history()` 逻辑。

确认无误后点「保存策略」→ 策略池中出现。

---

## 后续使用

打开策略配置，**基础参数**和**策略参数**分别独立：

```
┌─ 配置：强势反包 ──────────────────────────┐
│  名称 [强势反包             ] 显示上限 [30]│
│                                            │
│  📊 基础参数          [启用 ●]             │
│    价格 [3]~[200]元  排除ST、新股          │
│    最低成交额 [5000万]                     │
│                                            │
│  ⚙ 策略参数                               │
│    前日最大跌幅 [-0.02]                    │
│    成交量放大倍数 [1.2]                    │
│    反包容忍误差 [0.005]                    │
│                                            │
│  ⭐ 评分权重    📈 交易参数                 │
└────────────────────────────────────────────┘
```
