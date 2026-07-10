# 策略开发指南

本文档是策略开发的完整参考。人类开发者参考它编写策略；AI 运行时使用同目录下的 `strategy-guide-compact.md` 精简指南生成策略代码。

## 1. 策略文件格式

每个策略是一个 Python 文件，放在以下目录：

- 内置策略: `backend/app/strategy/builtin/`
- 自定义策略: `data/strategies/custom/`，建议文件名和 ID 使用 `custom_时间戳`
- AI 生成策略: `data/strategies/ai/`，文件名和 ID 使用 `ai_时间戳`

> ⚠️ **铁律**：AI/自定义生成的策略**只能**放入 `data/strategies/ai/` 或 `data/strategies/custom/`。严禁放入 `backend/app/strategy/builtin/`（内置策略目录，仅项目维护者可改），严禁借策略定制功能创建多个文件或修改任何项目源代码。

## 2. 文件结构模板

```python
"""策略简短描述"""
import polars as pl

META = {
    "id": "strategy_id",              # 英文ID, 唯一, 文件名同名；自定义策略建议 custom_时间戳
    "name": "策略中文名",              # 显示名称
    "description": "策略详细描述",     # 一句话说明策略逻辑
    "tags": ["标签1", "标签2"],        # 分类标签

    # 基础过滤参数 (Stage 1, 引擎统一处理)
    "basic_filter": {
        "price_min": 5,               # 最低价格
        "price_max": 200,             # 最高价格
        "market_cap_min": 20e8,       # 最小总市值 (元)
        "amount_min": 1e8,            # 最小成交额 (元)
        "exclude_st": True,           # 排除 ST/*ST/退市
        "exclude_new_days": 60,       # 排除上市N天内新股
    },

    # 策略参数 (只把用户可能调节的阈值放这里，公式常数不必参数化)
    # 每个参数含 id/label/type/default/min/max/step；select 类型用 options
    "params": [
    ],

    # 评分权重 (用于排序, 根据策略核心逻辑定制, 权重总和 = 1.0)
    "scoring": {
    },

    "order_by": "score",              # 排序字段, 通常用 "score"
    "descending": True,               # True = 从高到低
    "limit": 100,                      # 最多返回条数
}

# 买入信号 (回测 + 监控用, 根据策略逻辑选择合适的信号列)
ENTRY_SIGNALS = []

# 卖出信号
EXIT_SIGNALS = []

# 止损 (负数, 根据策略类型合理设定, 如做多短线 -0.05~-0.08)
STOP_LOSS = -0.05

# 最长持有天数 (短线 5~20, 中线 20~60)
MAX_HOLD_DAYS = 20

# 提醒条件 (监控用)
ALERTS = []


# 策略规则（人类可读，逐条编号，至少 3 条）
RULES = """
1. 规则描述一
2. 规则描述二
3. 规则描述三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    """策略核心过滤逻辑。

    df:     Stage 1 基础过滤后的 enriched 数据
    params: META.params 中定义的参数值 (用户可在前端覆盖)

    返回:   Polars 布尔表达式 (pl.Expr)
    """
    # 用 params.get("param_id", 默认值) 读取参数
    return (
        (pl.col("close") > pl.col("ma5"))
        & (pl.col("rsi_14") < 30)
    )
```

### 历史窗口策略（filter_history）

普通 `filter()` 只接收当前日期的单日数据。当策略需要以下逻辑时，必须使用 `filter_history()`：

- "最近 N 天内出现过某个事件"（如涨停、金叉）
- "某个事件发生后的第 X 天"（如涨停后放量下跌）
- "前高 / 前低 / 上次某事件的价格"等需要回溯历史的自定义字段
- 任何需要多日数据才能计算的时序逻辑

**不需要** `filter_history()` 的场景：只用当日指标列做比较（如 close > ma60、rsi_14 < 30）。

```python
LOOKBACK_DAYS = 8  # 回看交易日数，根据策略需要设置

def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """df 包含目标日期之前 LOOKBACK_DAYS 个交易日的数据（所有股票混合）。
    每行包含 symbol, date 及所有指标列/信号列。

    返回值: 筛选后的 DataFrame。
    重要: 返回所有匹配的行，不要只过滤最新日期，否则回测只有最后一天有信号。
    """
    if df.is_empty() or "date" not in df.columns:
        return df

    down_pct = float(params.get("prev_down_pct", -0.02))
    vol_ratio = float(params.get("volume_ratio", 1.2))
    tolerance = float(params.get("reversal_tolerance", 0.005))

    # 示例: 前日明显阴线下跌，今日放量阳线反包前日实体
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

    return hist.filter(
        (pl.col("_prev_close") < pl.col("_prev_open"))
        & (pl.col("_prev_change_pct") <= down_pct)
        & (pl.col("close") > pl.col("open"))
        & (pl.col("close") > pl.col("_prev_open"))
        & (pl.col("close") >= pl.col("_prev_high") * (1 - tolerance))
        & (pl.col("volume") >= pl.col("_prev_volume") * vol_ratio)
        & ((pl.col("close") > pl.col("ma5")) | (pl.col("close") > pl.col("ma10")))
    )
```

**关键要点：**
- `LOOKBACK_DAYS` 决定引擎加载多少天的数据，设为策略逻辑需要的最大回看天数
- 优先使用 Polars 的 `with_columns`、`over("symbol")`、`group_by`、`join`、`filter` 实现历史逻辑，避免把数据转成 Python list/dict 循环
- 只有遇到表达式难以描述的复杂状态机时，才使用 `partition_by("symbol")` + `to_dicts()` 逐股票分析
- **返回所有匹配行，不要过滤 `latest`**；选股引擎会自动取最新日，回测引擎需要全区间命中
- 未声明 `filter_history()` 的策略走普通 `filter()` 路径，不受影响

## 3. 常用指标列（参考，可直接使用）

以下列在数据中已预计算，可直接引用。**但如果这些列无法满足策略需求，可以不用，自行在 `filter_history()` 中基于 enriched 表的数据（已复权，含所有指标列和信号列）计算任何需要的字段。**

### 通用列

| 列名 | 类型 | 说明 |
|------|------|------|
| symbol | string | 股票代码 (如 600519.SH) |
| date | date | 交易日期 |

### 价格相关

| 列名 | 类型 | 说明 |
|------|------|------|
| open, high, low, close | float | OHLCV 开高低收 (前复权) |
| raw_close, raw_high, raw_low | float | 原始未复权价 |
| prev_close | float | 昨收价 |
| change_pct | float | 涨跌幅 (如 0.032 = +3.2%) |
| change_amount | float | 涨跌额 |
| amount | float | 成交额 |
| amplitude | float | 振幅 |

### 均线

| 列名 | 说明 |
|------|------|
| ma5, ma10, ma20, ma30, ma60 | 简单移动均线 |
| ema5, ema10, ema20, ema30, ema60 | 指数移动均线 |

### 技术指标

| 列名 | 说明 |
|------|------|
| macd_dif | MACD DIF 线 |
| macd_dea | MACD DEA 线 |
| macd_hist | MACD 柱状 |
| boll_upper, boll_lower | 布林带上/下轨 |
| kdj_k, kdj_d, kdj_j | KDJ 指标 |
| rsi_6, rsi_14, rsi_24 | RSI 相对强弱 |
| atr_14 | 平均真实波幅 |

### 量能

| 列名 | 说明 |
|------|------|
| volume | 成交量 |
| vol_ma5, vol_ma10 | 成交量均线 |
| vol_ratio_5d | 5日量比 |
| turnover_rate | 换手率 |

### 动量与波动

| 列名 | 说明 |
|------|------|
| momentum_5d / 10d / 20d / 30d / 60d | N日涨幅 |
| annual_vol_20d | 20日年化波动率 |
| high_60d, low_60d | 60日最高/最低价 |

### 涨跌停

| 列名 | 说明 |
|------|------|
| consecutive_limit_ups | 连续涨停天数 |
| consecutive_limit_downs | 连续跌停天数 |

### 运行时附加列（由引擎从 instruments 表 JOIN）

| 列名 | 说明 |
|------|------|
| name | 股票名称 |
| total_shares | 总股本 |
| float_shares | 流通股本 |

（`total_shares` 和 `float_shares` 用于 `basic_filter` 中计算市值：`close * total_shares`）

## 4. 常用信号列（参考）

信号列是布尔值，**必须**使用 `.fill_null(False)` 处理空值。同样仅供参考，根据策略含义自行选择匹配的。

| 列名 | 方向 | 说明 |
|------|------|------|
| signal_ma_golden_5_20 | 买入 | MA5 上穿 MA20 |
| signal_ma_dead_5_20 | 卖出 | MA5 下穿 MA20 |
| signal_ma_golden_20_60 | 买入 | MA20 上穿 MA60 |
| signal_macd_golden | 买入 | MACD 金叉 |
| signal_macd_dead | 卖出 | MACD 死叉 |
| signal_ma20_breakout | 买入 | 突破 MA20 |
| signal_ma20_breakdown | 卖出 | 跌破 MA20 |
| signal_n_day_high | 买入 | 60日新高 |
| signal_n_day_low | 卖出 | 60日新低 |
| signal_boll_breakout_upper | 中性 | 突破布林上轨 |
| signal_boll_breakdown_lower | 中性 | 跌破布林下轨 |
| signal_volume_surge | 中性 | 放量 |
| signal_limit_up | 买入 | 涨停 (依赖 instruments 表，部分环境不生成) |
| signal_limit_down | 卖出 | 跌停 (依赖 instruments 表，部分环境不生成) |
| signal_limit_down_recovery | 买入 | 跌停翘板 (依赖 instruments 表，部分环境不生成) |
| signal_broken_limit_up | 卖出 | 炸板 (依赖 instruments 表，部分环境不生成) |

> **注意**：涨跌停类信号需要 instruments 表（板块代码）才能计算。如果策略只用涨停判断，优先用 `consecutive_limit_ups >= 1`（稳定列，始终可用）。

此外，用户自定义信号（`data/user_data/custom_signals/`）以 `csg_` 前缀注入，也可在 filter() 中引用。

## 5. 不可用的数据（重要）

以下数据**不在** enriched DataFrame 中，策略代码中**不能**直接引用：

| 数据 | 说明 |
|------|------|
| 财务数据 (PE/PB/ROE/净利润/营收/资产负债等) | 存储在独立 financials 表，未 JOIN |
| 扩展数据 (概念/行业/人气排名/资金流向等) | 存储在 ext_data 目录，未 JOIN |
| 盘中实时数据 (分时价/五档盘口等) | 仅前端轮询使用 |

如需财务或扩展数据作为筛选条件，需先在系统层面完成 JOIN 再提供给策略（当前未实现）。

## 6. 规则

1. `filter()` 必须返回 `pl.Expr` (用 `&` `|` 组合布尔表达式)；`filter_history()` 返回筛选后的 `DataFrame`
2. 信号列使用 `.fill_null(False)` 处理空值
3. 用户可能调节的数值阈值通过 `params` 暴露；公式常数、固定窗口边界、一次性内部变量不必强行参数化
4. `scoring` 权重总和必须为 1.0
5. 遵循 A 股 T+1 规则 (当日买入次日才能卖出)
6. 只允许 `import polars as pl`，禁止 import 其他模块
7. 禁止使用 `open()`, `exec()`, `eval()`, `os`, `sys`, `subprocess`
8. **贴合用户需求优先**：第3/4节的指标列和信号列仅供参考，能用则用；如果用户需求需要自定义计算（如"前高""上次涨停价""N日内某个事件后X天"），直接在 `filter_history()` 中自行设计和计算，不需要局限于已有列
9. `filter_history()` 中优先用 Polars 向量化语法；仅在复杂状态机无法清晰表达时，才用 `partition_by("symbol")` 逐股票分析

## 7. 策略示例

### 强势反包

```python
"""强势反包 — 前日阴线下跌 + 今日放量阳线反包"""
import polars as pl

META = {
    "id": "strong_reversal",
    "name": "强势反包",
    "description": "前一日明显阴线下跌，今日放量阳线收复前一日阴线实体",
    "tags": ["反包", "短线", "放量"],
    "basic_filter": {
        "price_min": 3, "price_max": 200,
        "market_cap_min": 10e8, "amount_min": 0.5e8,
        "exclude_st": True, "exclude_new_days": 30,
    },
    "params": [
        {"id": "prev_down_pct", "label": "前日最大跌幅", "type": "float",
         "default": -0.02, "min": -0.10, "max": -0.005, "step": 0.005},
        {"id": "volume_ratio", "label": "成交量放大倍数", "type": "float",
         "default": 1.2, "min": 1.0, "max": 5.0, "step": 0.1},
        {"id": "reversal_tolerance", "label": "反包容忍误差", "type": "float",
         "default": 0.005, "min": 0.0, "max": 0.03, "step": 0.005},
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_5d": 0.3},
    "order_by": "score", "descending": True, "limit": 100,
}

LOOKBACK_DAYS = 2

ENTRY_SIGNALS = ["signal_broken_board_recovery"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 10
ALERTS = [{"field": "signal_broken_board_recovery", "message": "反包信号"}]

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
    return hist.filter(
        (pl.col("_prev_close") < pl.col("_prev_open"))
        & (pl.col("_prev_change_pct") <= down_pct)
        & (pl.col("close") > pl.col("open"))
        & (pl.col("close") > pl.col("_prev_open"))
        & (pl.col("close") >= pl.col("_prev_high") * (1 - tolerance))
        & (pl.col("volume") >= pl.col("_prev_volume") * vol_ratio)
        & ((pl.col("close") > pl.col("ma5")) | (pl.col("close") > pl.col("ma10")))
    )
```

## 8. 完整示例

见 [strategy-example.md](./strategy-example.md) — 从零创建强势反包策略的三步完整演示。
