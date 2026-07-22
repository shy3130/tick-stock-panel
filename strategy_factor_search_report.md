# 因子工厂原型：吸收 AlphaGPT 精华 + tickflow 真实数据验证

> 目标：评估 `imbue-bit/AlphaGPT` 能否与本项目结合，吸其精华、取长补短。
> 结论：**能结合，且是天作之合**。本报告附带一个已跑通的原型（`factor_dsl.py` + `run_factor_search.py`）。

## 1. AlphaGPT 到底是什么（纠偏）

它不是"LLM 写策略"，而是一个 **基于深度强化学习的自动因子工厂（auto factor factory）**，偏加密/链上（Solana meme、Jupiter、Uniswap-v4）。精华集中在 `model_core/` 不到 300 行：

| 模块 | 文件 | 作用 |
|---|---|---|
| 因子 DSL / 词表 | `vocab.py` / `ops.py` | 因子 = "特征 + 算子" 组成的 **RPN token 序列**（最大长度 12） |
| StackVM | `vm.py`（~40 行） | 栈式解释器，把 token 公式在特征张量上**向量化执行**成因子信号 |
| 生成器 | `alphagpt.py` | 因果 Transformer，**自回归生成公式 token**，用回测 Sharpe 做 RL 奖励 |
| 研究/执行分层 | `strategy_manager` / `execution` | 公式只产出 0~1 分数，交易层只管风控与下单 |

**它缺的，正是我们强的**：
- 严谨的 A 股回测引擎（它的 `backtest.py` 仅 1.2KB，远不如我们的 Polars 引擎）
- 真实 A 股数据（`kline_daily_enriched`）
- 我们已做完的 regime 风控研究（hard / soft / portfolio-rebalance 门控）

**两个重要诚实点**：
1. 真正的 RL 训练循环**不在仓库里**（`lord/experiment.py` 只是个 modular-addition 的 grokking 玩具），所以"直接跑它的训练器"不现实。
2. 它偏加密；特征（LIQ_SCORE/FOMO/PRESSURE）是链上特有的。我们只需把**特征叶子**换成股权特征，DSL+VM 架构原样可用。

## 2. 取长补短方案

```
AlphaGPT 供给（精华）          tickflow 供给（补短）
─────────────────────          ─────────────────────
因子 DSL + StackVM    ──────▶  严谨 Polars 回测引擎（评估器）
可搜索的因子表示      ──────▶  真实 A 股数据 + walk-forward OOS
"生成→执行→打分→优化"循环 ──▶  regime 风控（hard/soft/rebalance）
研究/执行分层思想     ──────▶   18 个已验证 MatrixStrategy + 信号框架
```

移植后的因子工厂 = **AlphaGPT 的发现语法 + tickflow 的验证纪律**。

## 3. 原型实现与结果（真实 A 股数据）

- `factor_dsl.py`：纯 numpy 移植 StackVM + 股权特征（RET / MA20_DEV / MA60_DEV / VOL_RATIO / MOM20 / MOM5 / RSI14 / AMP / TURN）+ 随机 RPN 生成器。
- `run_factor_search.py`：取 400 只 A 股样本（2025-01-01~2026-06-24），随机生成 1205 个公式，经 StackVM 横截面执行，按**横截面 IC / ICIR / Top-decile 多头日收益 Sharpe** 打分。36 秒跑完，253 个有效公式。

**种子公式（验证 DSL 能原样表达我们已有策略）：**

| 公式名 | RPN | IC | ICIR | Sharpe | 说明 |
|---|---|---|---|---|---|
| momentum20 | `MOM20` | +0.051 | +5.23 | +2.50 | 即我们的动量逻辑 |
| pullback_ma20 | `MA20_DEV` | +0.048 | +5.15 | +1.68 | 即缩量回踩支撑 |
| vol_breakout | `MOM5 VOL_RATIO MUL` | +0.036 | +4.26 | +0.86 | 放量突破 |
| trend_strength | `MA60_DEV MOM20 MUL` | +0.030 | +3.96 | +0.57 | 趋势强度 |
| mean_revert | `RET NEG` | **-0.037** | -3.78 | -3.49 | 日频均值回归无效（A 股动量主导） |

**IC 与 Sharpe 均为正的稳健因子 Top（更值得追）：**

| 公式 | IC | ICIR | Sharpe | RPN |
|---|---|---|---|---|
| rand_1002 | +0.057 | +6.20 | **+2.94** | `MA60_DEV VOL_RATIO DECAY MA60_DEV ADD ADD SIGN MA60_DEV MOM5 SUB ADD` |
| rand_107 | +0.064 | +6.12 | **+2.44** | `MA60_DEV MOM5 VOL_RATIO DELAY1 MOM5 SUB ADD MUL MA60_DEV ADD` |
| rand_497 | +0.058 | +6.49 | **+2.30** | `AMP RET ABS ADD MA60_DEV MUL DELAY1 DECAY VOL_RATIO DIV` |

全样本 56.5% 公式 IC>0（优于 50% 随机 → 特征/算子空间确有微弱预测力）。

## 4. 必须诚实的局限（不要当真金 alpha）

1. **样本内 + 选择偏差**：Top-of-1200 的 ICIR≈10 是多重检验幸存者偏差。这正是我们路线图里 **Deflated Sharpe** 要治的病——单因子 t 值需除以 sqrt(尝试次数)。
2. **未做 walk-forward OOS**：当前窗口同时用于生成与评估。必须切训练/测试段验证。
3. **Top-decile 多头是简化代理**：真实交易需接入我们的引擎（费用/滑点/持仓上限/regime 门控）。
4. **随机搜索粗糙**：未用 RL/遗传/LLM 引导，只证明"管道可行"。

## 5. 下一步（已具备基础设施）

1. **接入引擎**：把Top因子编译成 `custom_factor` MatrixStrategy，用我们的引擎（费用/滑点/regime）实盘式回测，替代简化 Sharpe。
2. **Walk-forward OOS + Deflated Sharpe**：训练段生成、测试段评估，对多因子组合做多重检验校正（沿用既有 regime 研究框架）。
3. **LLM 提议因子**：用 LLM 把市场叙事（"AI 硬件动量"）转成 RPN 公式，经 StackVM 编译、引擎验证——这是"吸其精华 + 我们的增强"。
4. **regime 感知选因子**：崩盘段切换到低 beta 因子，牛段切高动量因子（合并两项目最佳部分）。

---
*产物：`factor_dsl.py`（DSL+VM）、`run_factor_search.py`（搜索）、`strategy_factor_search.json`（253 公式打分）。*
