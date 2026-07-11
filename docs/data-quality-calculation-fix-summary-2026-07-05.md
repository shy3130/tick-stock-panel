# 数据计算口径修复总结（2026-07-05）

## 背景

本次修复来自单股日 K 与实时行情链路的数据口径复核，重点围绕：

- `change_pct`、`change_amount`、`amplitude` 的历史 / 实时计算口径一致性。
- `volume`、`float_shares`、`turnover_rate` 的单位契约。
- A 股涨跌停信号不能误用于 HK / ETF。
- 实时蜡烛覆盖不能混用新 OHLC 和旧衍生字段。

## 修复内容

### 1. 统一涨跌幅 / 涨跌额 / 振幅口径

历史全量路径 `compute_indicators()` 已改为使用复权后的 `close/high/low` 与 `prev_close` 计算：

- `change_pct`
- `change_amount`
- `amplitude`
- `_daily_pct`

这样和实时增量路径 `compute_enriched_today()` 保持一致，也和前端展示的复权 OHLC 保持一致。

### 2. 实时蜡烛覆盖补齐衍生字段

`_maybe_inject_live_candle()` 覆盖当天蜡烛时，现在同步覆盖：

- `change_pct`
- `change_amount`
- `amplitude`
- `turnover_rate`

避免 `close/high/low` 已更新，但信息栏仍显示旧的涨跌额或振幅。

### 3. 非 A 股只计算换手率，不生成涨跌停信号

`compute_all()` / `compute_enriched_today()` 中，`asset_type != "stock"` 时只通过 instruments 补算 `turnover_rate`，不进入 A 股涨跌停信号计算。

这保证 HK / ETF 在有 `float_shares` 时能得到换手率，同时不会出现 `signal_limit_up`、`signal_broken_limit_up` 等 A 股专属字段。

### 4. 单股本地路径按资产类型取 instruments

单股日 K、本地 on-demand 分析、实时 enriched 回退路径已按真实 `asset_type` 获取 instruments，避免只取 `stock` 导致 HK / ETF 缺失 `float_shares`。

## 验证

已跑相关回归：

```bash
cd backend && uv run --extra dev pytest \
  tests/indicators/test_pipeline_data_quality.py \
  tests/indicators/test_pipeline_hk.py \
  tests/api/test_kline_hk.py \
  tests/api/test_kline_local_fallback.py \
  tests/services/test_stock_analyzer_hk.py -q
```

结果：

```text
19 passed
```

前端类型检查：

```bash
cd frontend && pnpm tsc --noEmit
```

结果：通过。

另由 subagent `Peirce` 做了两轮只读复核：第一轮发现口径分裂和实时覆盖缺字段；修复后第二轮结论为 `Approve`。

## 注意

本次未做全量后端测试最终收口：一次 `pytest -q` 在约 40% 后长时间无输出，已终止。当前结论基于相关测试集、前端类型检查和 subagent 复核。
