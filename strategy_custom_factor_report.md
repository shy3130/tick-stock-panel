# P6：AlphaGPT DSL 因子接入真实回测引擎 —— 验证报告

> 把 `run_factor_search` 因子工厂产出的 Top 因子（StackVM 编译成 `custom_factor` 策略），用 tickflow **真实 Polars 回测引擎**（费用 0.02% / 滑点 5bps / 止损 -5% / 持仓≤20 日 / 仓位评分选股）实盘式回测，对照 2 个内置基准策略。
> 一句话结论：**DSL+VM 管道跑通了，但工厂的样本内因子是「多重检验的镜花水月」，接入真实引擎后崩盘。这反而证明了我们路线图（walk-forward OOS + Deflated Sharpe + regime 门控）的必要性。**

## 1. 实盘式回测结果（引擎原生，含费用/滑点/风控）

### 窗口 A：结构牛（2026-03-24 ~ 2026-06-24）

| 策略 | 因子公式 | 收益 | 笔数 | 胜率 |
|---|---|---|---|---|
| **bullish_alignment**（基准） | (builtin) | **+362.3%** | 108 | 22.2% |
| pullback_to_support（基准） | (builtin) | +246.5% | 75 | 36.0% |
| cf:MOM20 | `MOM20` | +260.5% | 120 | 16.7% |
| cf:MA20_DEV | `MA20_DEV` | +3.1% | 112 | 21.4% |
| cf:rand_1002（工厂Top） | `MA60_DEV VOL_RATIO DECAY MA60_DEV ADD ADD SIGN MA60_DEV MOM5 SUB ADD` | **-30.8%** | 115 | 19.1% |
| cf:rand_107（工厂Top） | `MA60_DEV MOM5 VOL_RATIO DELAY1 MOM5 SUB ADD MUL MA60_DEV ADD` | **-46.4%** | 123 | 17.9% |

### 窗口 B：普跌段（2025-09-24 ~ 2025-12-24）

| 策略 | 因子公式 | 收益 | 笔数 | 胜率 |
|---|---|---|---|---|
| **bullish_alignment**（基准） | (builtin) | **+196.6%** | 96 | 29.2% |
| pullback_to_support（基准） | (builtin) | -87.9% | 100 | 21.0% |
| cf:MOM20 | `MOM20` | -40.7% | 105 | 18.1% |
| cf:MA20_DEV | `MA20_DEV` | -92.8% | 98 | 15.3% |
| cf:rand_1002（工厂Top） | 同上 | **-87.7%** | 81 | 21.0% |
| cf:rand_107（工厂Top） | 同上 | **-96.6%** | 99 | 16.2% |

## 2. 样本内 vs 样本外：照妖镜

把因子工厂报告里的**样本内**（同一批近期数据上随机搜索打分）与本次**引擎实盘式**（OOS 性质，同引擎不同区间）摆一起：

| 因子 | 样本内 IC | 样本内 Sharpe | 引擎·结构牛 | 引擎·普跌段 | 判语 |
|---|---|---|---|---|---|
| rand_1002 | +0.057 | **+2.94** | -30.8% | -87.7% | 镜花水月，崩 |
| rand_107 | +0.064 | **+2.44** | -46.4% | -96.6% | 镜花水月，崩 |
| MOM20 | +0.051 | +2.50 | +260.5% | -40.7% | 真实但纯 beta，看天吃饭 |
| MA20_DEV | +0.048 | +1.68 | +3.1% | -92.8% | 弱，两头都不行 |

**关键发现：样本内 Sharpe 排名与引擎实盘排名完全相反。** 样本内最「漂亮」的 rand_1002/rand_107，恰恰是引擎里最惨的。这证明工厂报告第 4 节的预警完全成立——Top-of-1200 的 ICIR≈6、Sharpe≈3 是**多重检验幸存者偏差**，不是 alpha。

## 3. 诚实结论

1. **管道成功，因子失败。** `factor_dsl.py` 的 StackVM + 股权特征能原样编译 MOM20/MA20_DEV 等公式并接入引擎跑出真实交易（MOM20 在结构牛 +260.5%），说明「AlphaGPT 发现语法 + tickflow 验证纪律」这条路**技术可行**。
2. **随机搜索 + 样本内打分 = 灾难。** 工厂 Top 因子实盘崩盘，印证必须做 **walk-forward OOS + Deflated Sharpe**（单因子 t 值 ÷ √尝试次数）校正。
3. **MOM20 仍只是动量 beta。** 牛段 +260%、熊段 -41%，和内置 `pullback_to_support` 一样「看天吃饭」——这正是我们已建好的 **regime 硬/软门控**要治的病：MOM20 接入门控后熊段应被屏蔽。
4. **真正能打的还是 bullish_alignment**：两个窗口都正（+362% / +197%），是唯一穿越牛熊的基准。新因子若想取代它，必须先过 OOS + regime 两关。

## 4. 下一步（路线不变，但优先级被这次验证强化）

- **P7｜Walk-forward OOS + Deflated Sharpe**：训练段（如 2025H1）用 RL/遗传/LLM 生成因子，测试段（2025H2、2026H1）评估；Top 因子必须过 Deflated Sharpe 阈值才进引擎。这是把 rand_1002 式陷阱挡在门外的唯一办法。
- **P8｜regime 感知选因子**：MOM20 接硬门控（熊市清零 entry）后应显著减亏；崩盘段切低 beta 因子，牛段切高动量因子。
- **P9｜LLM 提议因子（吸精华升级版）**：把市场叙事（「AI 硬件动量」）经 LLM 转 RPN → StackVM 编译 → 引擎验证，闭环替代随机搜索。

---
*产物：`custom_factor.py`（内置策略，已随引擎加载）、`factor_dsl.py`（DSL+VM）、`run_custom_factor.py`（验证脚本）、`strategy_custom_factor_verify.json`（原始结果）。*
