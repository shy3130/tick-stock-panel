# C13：技术形态识别实现计划

> **面向 AI 代理的工作者：** 固定少量可测试形态。不要做缠论/波浪/主观大全。

**目标：** 从 Vibe `pattern_tool.py` 思路迁移一层轻量形态识别：峰谷、突破、平台整理、双底/双顶。输出可解释标签，供个股分析/回测筛选使用。

**现状证据：**
- panel 已有 `kline_daily_enriched`、ETF enriched 和个股分析页。
- C4 因子强调黄金对拍；形态同样需要固定口径测试，不能靠主观描述。
- 不引入 TA-Lib，避免新依赖。

**范围：** 纯 OHLCV 后处理；后端函数 + 可选 API。前端展示后置。

## 文件

| 文件 | 动作 |
|---|---|
| `backend/app/backtest/patterns.py` | 创建形态识别函数 |
| `backend/app/api/patterns.py` | 创建单股查询 API |
| `backend/app/main.py` | 注册 router |
| `backend/tests/backtest/test_patterns.py` | 创建 |
| `backend/tests/api/test_patterns.py` | 创建 |

## 任务 1：数据契约

- [ ] 输入 DataFrame 至少含：`date/open/high/low/close/volume`。
- [ ] 输出：

```json
{
  "pattern": "double_bottom",
  "date": "2026-07-01",
  "confidence": 0.74,
  "features": {"left_low": 10.1, "right_low": 10.3, "neckline": 12.0}
}
```

- [ ] confidence 是启发式，不当预测概率。

## 任务 2：失败测试 - pivots

- [ ] 单调上涨序列无 pivot high/low。
- [ ] 简单 `[1,3,1]` 有 high。
- [ ] 含 null 时跳过对应窗口，不抛异常。
- [ ] `window=5` 固定窗口，暂不暴露 UI。

## 任务 3：实现 pivots

- [ ] `find_pivots(df, window=5) -> list[dict]`
- [ ] pivot high：中心 high 为窗口最大且唯一。
- [ ] pivot low：中心 low 为窗口最小且唯一。
- [ ] strength：中心点相对窗口均值偏离幅度。

## 任务 4：突破与平台

- [ ] `detect_breakout(df, lookback=60)`
  - close 突破前 lookback high
  - volume ratio >= 1.2 加分，不强制
- [ ] `detect_consolidation(df, lookback=20, max_range_pct=0.12)`
  - `(max(high)-min(low))/last_close <= max_range_pct`
  - 输出 range_pct。
- [ ] 测试固定序列，避免随机。

## 任务 5：双底/双顶

- [ ] 双底：
  - 两个 pivot low 间隔 >= 5 交易日
  - 两低点价差 <= 5%
  - 中间反弹 >= 8%
  - 第二低点后 close 突破 neckline 才 confidence 高
- [ ] 双顶对称。
- [ ] 阈值写常量，不做配置。

## 任务 6：API

- [ ] `GET /api/patterns/{symbol}?lookback=120&asset_type=stock`
- [ ] repository 读取 enriched/raw 日线。
- [ ] 返回 `patterns` 列表 + `as_of`。
- [ ] 找不到数据返回空列表，不 500。

## 任务 7：AI 接入

- [ ] 个股分析只消费 pattern 摘要，不把完整 OHLCV 送 LLM。
- [ ] prompt 明确“形态标签为启发式，不构成预测”。

## 验证

```bash
cd backend
uv run --extra dev pytest tests/backtest/test_patterns.py tests/api/test_patterns.py -q
```

## 非目标

- 不做缠论、波浪、K线组合大全。
- 不新增技术指标依赖。
- 不承诺预测有效性。

