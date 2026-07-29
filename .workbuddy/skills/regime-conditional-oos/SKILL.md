---
name: regime-conditional-oos
description: 在 tickflow-stock-panel 量化项目中，对"regime 条件化策略"（牛段跑动量/熊段切 pullback 或空仓）做诚实的样本外验证。当用户要在真实回测引擎里接入 regime 切换策略、用 walk-forward OOS 对比 flat/switch、或避免 regime 信号实现陷阱（累计和 MA / 暖机窗口 / universe 漂移）时使用。覆盖引擎原生 regime、自包含 builtin 策略写法、2×2 归因与 F4 卡错腿根因。
---

# regime 条件化策略 · 诚实 OOS 验证流水线

本 skill 是 `quant-factor-oos` 的**策略级延伸**：因子级 OOS 判完 alpha 后，下一步就是"按市场状态切换策略"。本流水线负责把 regime 切换策略接入真实 Polars 引擎，并用 walk-forward OOS 回答一个诚实问题——**切换动作本身到底加不加值**，而不是靠样本内 Sharpe 自欺。

> 因子 DSL / RPN / 多重检验校正见 `quant-factor-oos`。本 skill 只讲 regime 切换 + 真实引擎集成。

## 何时用
- 用户想在真实引擎接入「牛→动量 / 熊→pullback 或空仓」类切换策略。
- 要跑 walk-forward OOS 对比 flat（熊市空仓）vs switch（熊市换策略）、不同信号源。
- 复刻/改写引擎 regime 信号时，避免踩累计和 MA、暖机、universe 漂移的坑。

## 关键文件（backend/ 下）
- `app/strategy/builtin/regime_conditional.py` —【核心参考】自包含 regime 切换策略：牛段跑 `mom_trend`(MOM20·SIGN(MA60_DEV))、熊段硬切 `pullback_to_support` 或置空、牛→熊翻转日强平 mom。参数 `bear_strategy`(pullback/flat)、`regime_source`(leader/ew)、`regime_ma`(20/60)。
- `app/strategy/builtin/factor_ensemble.py` — 6 语义动量因子横截面 z-score 等权平均（**反例**：等权稀释最优因子，见诚实结论）。
- `app/strategy/builtin/custom_factor.py` — mom_trend 载体（matrix_native）；`pullback_to_support.py` — 熊腿候选。
- `research/regime/run_regime_ensemble.py` —【权威脚本】8 配置 × 4 折 walk-forward：mom_trend / flat_leader / switch_leader / flat_ew / switch_ew / flat_ew20 / switch_ew20 / ensemble，含 2×2 归因。
- `research/regime/run_regime_conditional.py` — regime 第一版（5 配置，信号源混淆，已弃用）。
- `research/factors/run_factor_engine_wf.py` — **统一 universe/4 折切分口径源**：`N_SYM=400, SEED=20260723, FULL0=2024-09-24, FULL1=2026-06-30, N_FOLDS=4, TRAIN_SKIP_TD=80`。复用前先读此文件。
- `research/regime/diag_f4_regime.py` — F4 regime 信号诊断（输出 `artifacts/current/diag_f4_regime.json`）。
- `scripts/tushare_sync.py` — 安全增量 Tushare 同步；已有股票日线跳过，缺口校验后原子写入。
- `research/regime/market_structure.py` / `run_market_structure_v1.py` — P13 全市场结构牛熊因果标签与缓存。
- `research/regime/run_structure_strategy_replay_v1.py` — 趋势突破/均线多头 × 现金/回踩的七配置四折历史复验。
- `research/reporting/make_regime_ensemble_report.py` — 读 `artifacts/current/` 的 JSON 生成综合 HTML（Chart.js）。
- `app/backtest/strategy.py` — `compute_signals` 契约；暖机 `max(120, warmup_bars*1.6)`（line 873）；`regime_filter`→`regime_allow/bear_weight/scale_existing`（line 1084-1099, 1475-1538）。
- `app/backtest/engine.py` — regime 字段 `regime_allow / regime_bear_weight / regime_scale_existing`（line 74-80）；模拟循环软减仓（line 1931-1957）。

## 自包含 builtin 策略写法（硬约束）
- 所有策略（含 builtin）过 `ai_generator._validate_safety` 白名单：**只允许导入 `polars, numpy, app.backtest.matrix, datetime, __future__`**。
- **禁止跨 builtin 互 import** → DSL/算子必须内联（参考 `regime_conditional.py` 把 `factor_dsl.StackVM` 逻辑内联）。
- `compute_signals(market, params)`：传入的 `market` 矩阵**含暖机条**；`market.timestamp_labels` 是每根 bar 日期字符串（对齐 leader 信号用）。
- `make_signal_matrix(shape, *, entry, exit, score, entry_signal_code, exit_signal_code, ...)`：score 默认 0.0（float32）且**必须全部 finite**，否则 `validate_signal_matrix` 报错。
- **引擎暖机 = `max(120, warmup_bars*1.6)`**（strategy.py:873）。声称 60 根暖机实际吃 **120 根**——算 MA 窗口按 120 估，否则牛腿塌缩。

## 🔴 头号坑：regime 信号 MA 必须用 rolling_mean（曾因此全判熊）
- **症状**：自实现 leader 信号全判熊（仅 0.2% 牛），导致 `switch_leader ≡ switch_ew`（都卡熊）的**假象**，掩盖 2×2 真实差异。
- **根因**：手写 `c += level[i]` 但**从不减窗口首项** → MA 随 i 单调爆炸 → 永远判熊。
- **修复**：用 `pl.Series(level).rolling_mean(ma_win)`（polars 在白名单内）。引擎本就用同款，给 **~70% 牛**。
- **铁律**：复刻引擎 regime 信号时，MA **必须用 `rolling_mean`**，严禁手写累计和。
- **ew 等权信号 0% 牛是真实样本属性，非 bug**：等权 400 只篮子在本区间持续低于 MA60/MA20（横盘阴跌），而 leader 龙头指数涨 ~10% → **龙头 vs 等权明显背离**。引擎 `market.close` 不前向填充，自算指数需**逐标的前向填充**后再取等权均值，否则信号被缺口扭曲更弱。

## universe 漂移（可比性陷阱）
- `random.sample(SEED)` 抽 400 只；`data/kline_daily_enriched` 的 parquet 集随项目增长 → 两次运行抽到**不同 universe** → 绝对收益（如 B 方案 +9.46% vs 后来 -2.25%）**不可跨 run 对照**。
- **规则**：结论一律基于「同一次运行内多配置可控对比」，只比相对排序与方向。

## 引擎原生 regime vs 自实现切换策略
- **引擎 regime 只有软减仓，不换策略**：`regime_allow`(逐日牛熊) + `regime_bear_weight`(熊市日敞口缩放) + `regime_scale_existing`(把整本书减持到 regime 目标)。要「熊市空仓」用 `{type:leader_index, ma:60, bear_weight:0, scale_existing:True}`。
- **硬切换（牛/熊腿不同策略）必须新建 builtin 策略**：在 `compute_signals` 里现场算 regime 掩码，分别调两腿信号再按掩码合并（`entry = (bull & legA) | (bear & legB)`），并在牛→熊翻转日对仍持仓的牛腿发 exit 信号。
- **信号源统一才干净**：2×2 归因 = 信号源(leader/ew) × 熊市动作(flat/switch)。若 switch 用 ew、flat 用 leader，差异混了「信号」与「动作」，无法归因（第一版就踩了）。

## walk-forward 2×2 归因配方
1. 固定 universe + 4 折切分（复用 `research/factors/run_factor_engine_wf.py` 口径）。
2. 跑 8 配置：`mom_trend`（无regime基线）/ `flat_leader`+`switch_leader`（leader 信号 × 空仓/切pullback）/ `flat_ew`+`switch_ew`（ew 信号 × 空仓/切pullback）/ `flat_ew20`+`switch_ew20`（ew MA20 响应式，让两腿都部署）/ `ensemble`。
3. 归因块：`action_effect = switch - flat`（同信号源下，隔离「切换动作」价值）；`signal_effect = leader - ew`（同动作下，隔离「信号源」价值）。
4. 跑法：`cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.regime.run_regime_ensemble > ../artifacts/logs/regime_ensemble_run.log 2>&1`。

## 诚实结论（权威 run，同 run 内对比）
| 配置 | 均值收益 | Sharpe | MDD | 正折 |
|---|---|---|---|---|
| flat_leader（牛mom/熊空仓·leader） | -10.44% | -1.03 | -20.45% | 1/4 |
| mom_trend（无regime） | -3.65% | -0.12 | -20.63% | 1/4 |
| switch_leader（熊切pullback） | -8.63% | -0.61 | -23.76% | 1/4 |
| switch_ew（熊切pullback·ew） | +0.27% | +0.21 | -16.73% | 3/4 |
| ensemble（6因子等权） | -7.95% | -0.46 | -22.42% | 1/4 |

- 旧的 `flat_leader`“当前最强”结论已因 universe 非确定性撤回；上表是排序采样后的 canonical replay。
- 当前 8 配置没有一个通过稳健晋级门槛；`switch_ew` 虽 3/4 正折，但 F4 -17.19% 且信号退化。
- P13 结构标签能力已实现，但趋势突破/均线多头在结构熊切现金或回踩都只在 1/4 折战胜全时段基线，全部拒绝。
- 能区分状态不等于切换策略有收益；不得为满足“牛熊必须不同策略”而强制启用。

## F4 炸裂根因（已坐实，非 whipshaw）
- `switch_ew` 在 F4 亏 -17.19%。根因 = **ew MA60 信号滞后**：等权指数 F4 实际 +5.6% 但全程在 MA60 下方（均值 |偏离| 14.91%，最大 30.40%）→ 100% 判熊、0 翻转 → 策略整段只部署 pullback 熊腿。
- 对照：`leader` 信号 F4 有 **88.5% 牛**、5 次翻转，`switch_leader` F4 为 -3.88%。
- 启示：问题在「ew 信号滞后」，不在「切换」动作 → 修法是给熊腿加短窗趋势确认（见下一步 ③）。

## 给同事的下一步
- Tushare 已补齐 2024-04-01~2026-07-27；2026-07-28 空响应已显式记录。后续只做
  安全增量补数，默认不得删除旧日线。
- P13 已使用前置行情暖机并重跑；研究期无 warmup。不要在现有四折反向扫描阈值
  或交换牛熊腿。
- 策略结论只能等待新的 60–120 个交易日观察。结构标签可以用于描述和归因，当前不得生产启用。

## 速查命令
```bash
cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.regime.run_regime_ensemble > ../artifacts/logs/regime_ensemble_run.log 2>&1
cd backend && .venv/Scripts/python.exe -m research.regime.diag_f4_regime
cd backend && .venv/Scripts/python.exe -m research.reporting.make_regime_ensemble_report
cd backend && .venv/Scripts/python.exe -m research.regime.run_market_structure_v1
cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.regime.run_structure_strategy_replay_v1
cd backend && .venv/Scripts/python.exe -m scripts.tushare_sync --start 20240401 --index-start 20240401 --workers 8
cd backend && .venv/Scripts/python.exe -m py_compile app/strategy/builtin/regime_conditional.py app/strategy/builtin/factor_ensemble.py research/regime/run_regime_ensemble.py
```

## 交付物约定
- 脚本按职责放 `backend/research/`，当前权威 JSON 与报告放 `artifacts/current/`，历史结果放 `artifacts/archive/`，提交 git。
- 项目交接文档 `HANDOFF.md`（项目根）同步维护。
