# 历史面板策略指南（已归档）

> 本文包含已下线 UI 的操作说明，只用于追溯。当前策略开发规范见 `docs/strategy.md`。

策略是选股引擎、回测、监控的基础。本文介绍策略体系与三种扩展方式。

完整策略开发规范(AI 生成与手写)见 [`backend/app/strategy/prompts/strategy-guide.md`](../../../backend/app/strategy/prompts/strategy-guide.md)。

---

## 内置策略

代码库保留 **22 个内置策略**，但默认策略目录只展示3个核心策略：

| 默认核心策略 | 定位 | 证据状态 |
| :--- | :--- | :--- |
| `bullish_alignment` | 趋势筛选 | historical replay 未晋级 |
| `trend_breakout` | 趋势突破 | historical replay 未晋级 |
| `pullback_to_support` | 回踩 | historical replay 未晋级 |

其余实现没有删除，而是按产品生命周期分层：

- `tool`：`custom_factor`，是 AlphaGPT DSL 因子工具，不是已验证独立 alpha。
- `experimental`：`factor_ensemble`、`regime_conditional`、`oversold_reversal`、
  `limit_up_momentum`、`quality_momentum_v1`，未通过 fresh OOS，默认隐藏。
- `legacy`：13 个重复度较高的启发式模板，保留直接调用和历史兼容，默认隐藏。
- `user`：用户自建和 AI 策略，默认可见，但证据状态始终从 `unverified` 开始。

所有实现均位于 `backend/app/strategy/builtin/`，基于 Polars/NumPy 向量化执行：

| 类型        | 代表策略                                                 |
| :---------- | :------------------------------------------------------- |
| 趋势 / 形态 | 趋势突破 · 均线多头 · MA 金叉 · MACD 金叉放量 · 布林突破 |
| 量价 / 涨停 | 量价齐升 · 高换手强势 · 连板股 · 断板反包 · 涨停动量 · 接近涨停 |
| 反转 / 波动 | 超跌反弹 · 超卖反转 · 新低反转 · 低波动龙头 · 回踩 MA20 · 回踩支撑 · 强势开盘 |

内置目录 `backend/app/strategy/builtin/` 由项目维护,**AI 生成的策略不会落入此目录**。

生命周期事实源是 `backend/app/strategy/catalog.py`。`GET /api/strategies` 和
`GET /api/screener/strategies` 默认返回核心策略及用户策略；传
`include_experimental=true` 才返回所有生命周期。两个批量运行入口也遵循同一默认，
但显式传入任何 `strategy_id` 仍可运行隐藏策略。

历史五策略协议已按固定 canonical universe 做7折训练内参数搜索和相邻测试段冻结复验，
结果全部未通过晋级门，生产默认参数未改。审计结果在
`artifacts/archive/optimization/core_strategy_walkforward_v1.json`；这些区间是历史
replay，不是 fresh OOS，不能用累计收益最好的一项冒充已经验证。

仓位结构也已独立比较等权/评分加权与 3/5/10/20 只持仓。趋势突破历史累计由
-12.53% 改善到 +0.75%，但只在 3/7 折战胜默认且训练选择不稳定，仍未晋级；其他
core 同样未通过。因此当前默认仍是 10 只等权，不能为了提高旧回测曲线擅自替换。

2026-07-01 起的冻结观察目前只有 15 个交易日：超卖候选 -10.19%（默认 -0.03%），
趋势突破20只等权 -2.30%（默认10只等权 -4.60%）。两者都没有正收益，且样本长度
不足，状态固定为 `PENDING_DATA`；生产默认仍不变。

退出与风控层也已比较7个固定候选。均线多头历史累计改善至 +6.76%，但只有4/7
正折、4/7战胜默认，未达到至少5/7的门槛；其他核心策略同样失败。没有新增生产
止损、持仓或移动止盈覆盖。

P12 又验证了市场宽度保护：用前一日站上MA20/MA60的股票比例做迟滞软减仓。训练选择
累计 -23.34%，默认 -0.90%，说明宽度门控错杀了趋势收益，已拒绝且默认关闭。

P13 将“2024-09-24 之后的大级别牛市”进一步拆成结构牛市和结构熊市。标签只用
前一交易日全市场数据，依据站上 MA20/MA60 的股票比例与 20 日等权复合收益判断，
并使用迟滞和两日确认。后端支持结构牛腿/结构熊腿各运行一个 matrix-native 策略，
翻转时退出旧腿；不需要新增前端。

滚动特征使用 2024-04-01 起行情暖机，标签只从 2024-09-24 起输出；因此研究期
不再包含人为的 MA60 warmup 段。当前为结构牛 191 日、结构熊 253 日，最新有效
标签是 2026-07-27 的结构熊，且只读取到前一交易日数据。

但历史四折不支持立刻启用切换：趋势突破全时段复合 +11.88%，切结构熊现金为
-3.27%、切回踩为 -8.32%；均线多头的两种切换也都比基线差。所有切换方案只在
1/4 折战胜相应基线，生产默认关闭。**市场状态可描述，不等于状态切换能提高收益。**

---

## 扩展策略的三种方式

### 🎛️ 方式一:自定义信号(不写代码)

在选股页 UI 上用 `字段 + 操作符 + 阈值` 组合,编译成 Polars 表达式热加载。适合:

- 快速验证一个简单的筛选思路(如 `RSI < 30 AND 量比 > 2`)
- 不熟悉 Python 但想自定义筛选条件

底层实现在 `backend/app/strategy/custom_signals.py`。

### 🤖 方式二:AI 生成

一句话描述思路,LLM 读取精简运行时指南生成完整策略文件:

1. **配置 AI 接口**(留空即关闭,见 [configuration.md → AI](../../configuration.md#ai可选)):
   ```ini
   AI_PROVIDER=openai_compat
   AI_BASE_URL=https://api.deepseek.com/v1
   AI_API_KEY=sk-...
   AI_MODEL=deepseek-chat
   ```
2. 在选股页打开「AI 策略生成器」,用自然语言描述你的策略思路
3. 前端流式接收生成代码,后端经 `ast` 安全校验(禁止 import os/sys/subprocess 等危险模块)后返回结果
4. 保存后落入 `data/strategies/ai/`,文件名/ID 用 `ai_` 前缀

生成策略相关提示词位于 `backend/app/strategy/prompts/`:

- `strategy-guide-compact.md` — AI 运行时精简指南(用于降低长请求超时概率)
- `strategy-guide.md` — 完整策略开发规范(供人工开发和详细参考)
- `strategy-builder-step2.md` — 步骤 2 提示词模板(修改已有策略)
- `strategy-example.md` — 从零创建强势反包策略的三步演示

> 💡 **文件与范围铁律**:AI 生成的策略只生成一个 `.py` 文件,只 `import polars as pl`,绝不修改 `backend/`、`docs/`、`artifacts/` 等现有文件。

### 📝 方式三:自定义编写 / 代码迁移

可以在选股页「自定义编写」中直接编辑策略代码并保存,新建自定义策略会落入 `data/strategies/custom/`,文件名/ID 用 `custom_` 前缀。也可以手动把已有策略改写为 Polars 文件后放入该目录,引擎会自动发现。

手写策略需遵循 [`strategy-guide.md`](../../../backend/app/strategy/prompts/strategy-guide.md) 的文件结构(META / basic_filter / scoring / ENTRY_SIGNALS / filter 等),完整规范见该文档。

---

## 策略文件结构(简述)

一个策略 `.py` 文件通常包含:

| 部分 | 作用 |
| :--- | :--- |
| `META` | 策略元信息(名称、参数、方向等),用户可在 UI 调整阈值 |
| `basic_filter(df, params)` | 模式 A:单日过滤,返回 `pl.Expr` |
| `filter_history(df, params)` | 模式 B:历史窗口过滤,返回 `pl.DataFrame`(配 `LOOKBACK_DAYS`) |
| `scoring` | 评分权重,总和 = 1.0 |
| `ENTRY_SIGNALS` / `EXIT_SIGNALS` | 进出场信号列(回测用) |

完整字段说明与示例见 [`strategy-guide.md`](../../../backend/app/strategy/prompts/strategy-guide.md)。

---

## 组合已有策略与自定义因子（后端 v1）

`POST /api/backtest/strategy/run` 可传可选的 `composition`。首个 component 是主策略，
必须与顶层 `strategy_id` 一致；它负责组合的止损、持仓上限和敞口规则。示例：保留均线
多头的选股门槛，再用 AlphaGPT DSL 自定义因子参与排序。

```json
{
  "strategy_id": "bullish_alignment",
  "start": "2024-01-01",
  "end": "2025-12-31",
  "max_positions": 10,
  "composition": {
    "entry_mode": "and",
    "score_mode": "weighted_rank",
    "components": [
      {"strategy_id": "bullish_alignment", "weight": 0.3},
      {
        "strategy_id": "custom_factor",
        "weight": 0.7,
        "params": {"factor_formula": "MOM20 VOL_RATIO MUL"}
      }
    ]
  }
}
```

- `entry_mode="and"`：所有 component 都允许才开仓，适合“策略筛选 + 因子排序”。
- `entry_mode="or"`：任一 component 允许即开仓，会扩大候选池。
- score 先在每个交易日做截面百分位再加权，避免不同量纲直接相加。
- 任一 component 发出退出信号即退出；v1 只支持 2–8 个 matrix-native 策略和正权重。

注意：`custom_factor` 对公式结果为有限值的资产都会给 entry。因此它与其他筛选策略组合时
一般应使用 `and`；使用 `or` 往往会退化为“几乎全市场由因子排序”。组合接口可运行不等于
组合有效，权重和公式仍必须经过 walk-forward/OOS 验证。

---

## 新增内置策略(贡献者)

如果你想为项目贡献一个内置策略：在 `backend/app/strategy/builtin/` 参照现有文件实现
`StrategyDef`，并在 `backend/app/strategy/catalog.py` 明确归类。未归类的新 builtin 会
按 `legacy/unverified/hidden` 安全默认加载，不会自动进入核心目录。
