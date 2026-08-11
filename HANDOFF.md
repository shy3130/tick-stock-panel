# tickflow-stock-panel 项目交接文档

> 最近审计：2026-08-09 ｜ 交接人：本会话主理人 ｜ 接手人：＿（同事）
> 定位：A 股量化策略因子研究 + 真实回测引擎的 **样本外（OOS）诚实验证** 项目。核心目标不是「做出漂亮曲线」，而是用 walk-forward + 多重检验判定因子/策略是否真有 alpha。

---

## 0. 一句话现状

已完成 P6–P10、P11-A、P11-B、P11-C 前置、P11-C2、P11-D 和 P11-E：因子 DSL、语义/多重检验、多区间 walk-forward、regime 条件化，以及
**AlphaGPT 闭环 v1**（合法 token 环境、候选池、训练折稳健奖励、进化搜索 vs 同预算
随机基线、断点、封存 HOLDOUT）与 policy adapter/离线 rollout 数据集。当前仍是
CPU-only 基线；纯 NumPy masked Transformer、奖励条件化 BC 和公式 reward
reranker 均已完成验证。P11-D 模型的封存 validation 排序 gate 通过，但新 seed
前瞻 reranker 的绝对奖励 gate 未通过，因此尚未接入搜索，也不含 PPO。
P11-E 已用 6 个全新随机公式 seed 采集 236 个标签并做 pairwise/listwise
seed-level CV；锁定 validation 排序同样失败，进一步说明仅靠公式 token 结构不足以
稳定预测随机公式奖励。没有运行 P11-E 前瞻 reranker。
**AlphaGPT Research v1.0 已冻结为完整可交接研究版本**：15 个必需产物、哈希、
数据边界和 gate 状态由统一发布入口验证。完整不等于生产可用；后续调优另开阶段。
**2026-07-26 已撤回 `flat_leader`“当前最强”结论。** 原 P8/P9/B/regime 脚本在
Polars `unique()` 的非确定顺序上直接做 seeded sample；三个独立进程得到三个不同的
universe 哈希，固定 seed 并未固定股票池。修复为“symbol 排序后采样”并重跑后，
`flat_leader` 四折均值为 **-10.44%、1/4 正折**。P7/P8/P9/B/regime/组合研究链现已全部
在共享 deterministic universe 契约上重跑；当前没有策略通过稳健晋级门槛。

生产回测侧已增加**矩阵策略组合 v1**：自定义因子不再只能作为独立策略运行，可以作为
既有矩阵策略的排序层；组合支持 AND/OR 开仓、截面百分位加权评分和任一子策略退出。
固定 30/70 的组合历史复验和 15 日未见观察已完成；部分相对差值为正，但绝对收益与
跨期一致性不足，仍不能宣称组合后稳定更高。晋级门状态为 `PENDING_DATA`（最低 60 日，
目标 120 日，当前还差 45 日），且即使数据够长也只进入冻结复核，不会自动晋级。
生产策略目录已从“全部暴露”先收敛为5个、现进一步收敛为3个默认 core：均线
多头、趋势突破、回踩支撑。超卖反转、涨停动量因入场/仓位/退出多层复验持续失败，
降为 experimental；实现和显式 ID 保留兼容。该分类代表产品入口去重，**不代表3个
core 已通过收益晋级**。历史五策略协议已完成统一的 train-only 参数 walk-forward：
7折全部使用相同
canonical 400 股 universe 和预算，五者均为 `REJECTED_HISTORICAL_REPLAY`，因此没有
改生产默认参数。超卖反转候选累计 +24.76%，但正折/战胜默认都只有 4/7，仍不合格。
随后完成仓位结构 v1：固定比较 7 个等权/评分加权持仓候选。趋势突破累计从默认
-12.53% 改善到 +0.75%，但只有 4/7 正折、3/7 战胜默认，训练赢家也不稳定；五个
策略再次全部拒绝，没有为了做高历史收益而改生产配置。
冻结前向观察已从 2026-07-01 启动：截至 2026-07-21 只有 15 个交易日。超卖候选
-10.19% 对默认 -0.03%；趋势突破20只等权 -2.30% 对默认10只等权 -4.60%。状态为
`PENDING_DATA`，距最低 60 日还差 45 日；趋势只是相对少亏，不能称为盈利改善。
退出/风控层随后固定比较7个候选：均线多头从默认 -0.90% 改善到 +6.76%，但正折与
战胜默认均为4/7，未达到至少5/7；涨停动量从 -33.09% 改善到 -3.68%，仍亏损。
五个 core 全部拒绝，没有建立 2026-07-22 后的新观察候选。
P12 市场宽度保护采用前一日MA20/MA60宽度、迟滞与既有软减仓通道。固定三候选的
训练选择累计 -23.34%，默认 -0.90%；仅2/7正折、3/7战胜默认。实现未退化，但门控
错杀趋势收益，已拒绝、不补扫阈值、默认关闭。
P13 已把 2024-09-24 后的大级别行情拆为因果的结构牛/结构熊标签。特征使用
2024-04-01 起行情暖机，研究期 454 日中结构牛191日、结构熊263日、无 warmup、
20次切换；最新 2026-08-10 为结构熊，标签只读取到前一交易日 2026-08-07。
双腿矩阵组合和翻转退出已实现，但四折历史复验不支持启用：
趋势突破全时段复合 +11.88%，结构熊现金 -3.27%，结构熊回踩 -8.32%；均线多头
三者分别 -12.27%/-30.65%/-32.82%。四个切换候选都只在1/4折战胜基线，状态
`REJECTED_HISTORICAL_REPLAY`，生产默认关闭。
P14 已于 2026-07-29 冻结注册“趋势突破始终运行”和“回踩支撑始终运行”的
前向观察。由于 7 月既有数据在注册前已可见，不能冒充 fresh OOS；门槛只从
2026-07-30 起计算。当前 0/60 日、`PENDING_DATA`、无失败，永不自动晋级。
结构牛市 60% 胜率 / 80% 收益挑战已做 30 个固定候选的诚实审计：在已经反复查看的
2026-03-24~06-24 目标窗口，均线多头的 4 只评分加权、5.5% 止盈、8% 止损达到
65.36% 胜率和 +95.94% 收益；但冻结后在两个既有 2025 强牛窗口分别为
51.43%/-16.94% 和 48.13%/-39.87%。状态 `REJECTED_OVERFIT`，没有写回生产。
本地 Tushare 股票日线现有3,094,338行、5,635只、572个交易日，覆盖
2024-04-01~2026-08-10且无重复键；enriched 行数与唯一键完全一致。安全增量同步
默认绝不删除旧日线。2026-08-10 收盘后补入5,538行，daily_basic与7条指数日线同步
成功；当日集合竞价另存独立数据集。

---

## 1. 环境与运行约定（必读）

| 项 | 说明 |
|---|---|
| Python | 项目自带 `backend/.venv/Scripts/python.exe`（Windows）。**不要**用全局 python。 |
| 数据位置 | `data/kline_daily_enriched/**/*.parquet`（**项目根 `data/`，不是 `backend/data/`**）。脚本用 `HERE.parent / "data"` 定位，从 `backend/` 用相对 `data/` 会解析到空目录（已踩坑）。 |
| Tushare 补数 | `cd backend && .venv/Scripts/python.exe -m scripts.tushare_sync --start 20240401 --index-start 20240401 --workers 8`；默认跳过旧分区，禁止用 `--clear-first` 做日常补数。 |
| 回测模式 | 必须 `TICKFLOW_BACKTEST_MODE=inprocess`（Windows 下 spawn 会崩）。 |
| 跑法范例 | `cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.regime.run_regime_ensemble > ../artifacts/logs/regime_ensemble_run.log 2>&1` |
| 新策略白名单 | **builtin 策略也过 `ai_generator._validate_safety`**，只允许导入 `{polars, numpy, app.backtest.matrix, datetime, __future__}`。**禁止跨 builtin 互 import**，必须自包含（参考 `regime_conditional.py` / `factor_ensemble.py` 把 DSL 内联）。 |
| 引擎暖机 | `warmup_days = max(120, warmup_bars*1.6)`（strategy.py:873）。声称 60 根暖机实际吃 **120 根**——算 MA 窗口时务必按 120 估。 |

---

## 2. 文件地图

### 因子 DSL 与引擎
- `backend/research/common/factor_dsl.py` — RPN 因子 DSL（纯 numpy StackVM）。**词表无常量字面量**：因子只能是 9 特征(RET/MA20_DEV/MA60_DEV/VOL_RATIO/MOM20/MOM5/RSI14/AMP/TURN) + 12 算子(ADD/SUB/MUL/DIV/NEG/ABS/SIGN/GATE/JUMP/DECAY/DELAY1/MAX3) 的纯组合，写不出阈值。
- `backend/app/backtest/matrix.py` — `make_signal_matrix(shape, *, entry, exit, score, entry_signal_code, exit_signal_code, ...)`。score 默认 0.0（float32）且必须全部 finite，否则 `validate_signal_matrix` 报错。
- `backend/app/backtest/strategy.py` — `compute_signals(market, params)` 契约；传入的 `market` 矩阵**包含暖机条**，`market.timestamp_labels` 是每根 bar 的日期字符串（对齐 leader 信号用）。
- `backend/app/backtest/engine.py` — 模拟主循环；regime 字段 `regime_allow / regime_bear_weight / regime_scale_existing`（line 74-80）。**本质是软减仓，不是换策略**。

### 策略载体（builtin）
- `backend/app/strategy/catalog.py` — 22 个 builtin 的生命周期事实源；默认 core 只有
  `bullish_alignment`、`trend_breakout`、`pullback_to_support`。`oversold_reversal`、
  `limit_up_momentum`、`quality_momentum_v1` 均为 experimental/hidden。core 是产品
  入口，不是收益晋级名单。
- `custom_factor.py` — `tool`；AlphaGPT DSL 因子/组合组件，非已验证独立 alpha。
- `regime_conditional.py` / `factor_ensemble.py` — `experimental`；historical replay
  未通过，默认隐藏但显式 ID 仍可回测。
- 其余 13 个重复度较高的启发式策略为 `legacy`，保留兼容、默认隐藏。
- `backend/app/strategy/composition.py` — 生产回测组合协议；复用已加载的 matrix-native
  策略，不创建第二套 builtin，也不依赖 `research/`。
- `backend/app/backtest/strategy.py` — `StrategyBacktestConfig.composition` 的依赖合并、
  单次行情准备、子策略 pipeline 执行和组合信号接入。

### 运行脚本（walk-forward OOS）
- `backend/research/alphagpt/` — P10 AlphaGPT v1；`environment.py`/`pool.py`/
  `reward.py`/`evolution.py` 是后续 policy 的稳定契约。
- `backend/research/alphagpt/run_alphagpt_v1.py` — 固定 seed/universe，T1-T3 搜索，
  HOLDOUT 在排名冻结后才读取；输出同预算 random/evolution 对照。
- `backend/research/alphagpt/policy.py` — `TokenPolicy` 与 `MaskedLogitPolicy`；
  后续模型只能通过这里使用 action mask。
- `backend/research/alphagpt/rollouts.py` / `dataset.py` — evolution 训练候选的逐
  token 教师重放、确定性 train/validation split、JSONL 与 manifest。
- `backend/research/alphagpt/run_rollout_collection.py` — P11-A 采集入口，不读取
  P10 最终候选的 HOLDOUT 报告。
- `backend/research/alphagpt/behavior_clone.py` / `run_behavior_clone.py` — P11-B
  n-gram、纯 NumPy Transformer、early stopping、生成审计和同预算训练奖励对照。
- `backend/research/alphagpt/run_rollout_expansion.py` — 固定 T1–T3 的多 seed
  evolution 扩容、跨 seed 去重和断点续跑。
- `backend/research/alphagpt/run_behavior_stability.py` — 多模型 seed validation、
  多样性、训练奖励和 pre-PPO gate。
- `backend/research/alphagpt/run_reward_conditioned_stability.py` — 固定配置的
  reward-weighted / top-40% elite BC 多 seed 对照。
- `backend/research/alphagpt/reward_model.py` / `run_reward_model.py` — P11-D
  558 维固定公式结构特征、纯 NumPy ridge、train-only CV、validation
  Spearman/top-k/calibration gate 和确定性 checkpoint。
- `backend/research/alphagpt/reranker.py` / `run_reward_reranker.py` — 全新 seed
  的未见候选池预筛，与同候选池随机选择做严格同预算 T1–T3 对照。
- `backend/research/alphagpt/reward_labels.py` /
  `run_reward_label_expansion.py` — P11-E 随机公式标签、seed-level split、
  intrinsic/operational reward 分离和失败审计。
- `backend/research/alphagpt/rank_model.py` / `run_rank_model_v2.py` — pairwise /
  listwise 线性排序、train seed 留一 CV 和锁定 validation gate。
- `backend/research/alphagpt/release.py` / `run_release_v1.py` — v1.0 发布 manifest、
  15 个必需产物哈希和跨阶段边界校验；`--verify-only` 用于接手验收。
- `backend/research/common/universe.py` — 所有研究脚本共享的稳定排序采样、完整 symbol
  manifest、SHA-256 与原子写入实现。
- `backend/research/factors/run_factor_engine_wf.py` —【canonical】B 方案五配置对照；
  `mom_trend` 仅 1/4 正折、均值 -3.65%。
- `backend/research/regime/run_regime_conditional.py` —【canonical】regime 第一版（5 配置）。
- **`backend/research/regime/run_regime_ensemble.py`** —【最新·canonical】8 配置 4 折；先排序再采样并记录 universe SHA-256。结果是“无策略晋级”，不是 flat_leader 获胜。
- `backend/research/validation/run_strategy_composition_wf.py` — 组合 v1 固定协议复验；含历史复验、15 日未见观察、完整 universe manifest、协议哈希、断点、相对差值和 fresh-OOS 晋级等待门。
- `backend/research/optimization/run_core_strategy_walkforward_v1.py` — 五个 core 的统一
  train-only 参数搜索；7 折、固定预算、默认参数配对、失败显式记录和无信号持币语义。
- `backend/research/optimization/run_core_portfolio_walkforward_v1.py` — 五个 core 的仓位
  结构搜索；每折固定 7 个候选、训练内选择、测试只评估、严格 JSON 与断点复跑。
- `backend/research/validation/run_core_strategy_forward_watch_v1.py` — 两个冻结候选的
  前向观察；固定校准截止日、协议哈希、60/120 日门槛和永不自动晋级规则。
- `backend/research/optimization/run_core_exit_walkforward_v1.py` — 五个 core 的退出/
  风控搜索；固定7候选、共享只读行情矩阵、训练内选择和7月22日后观察边界。
- `backend/research/optimization/run_bullish_breadth_walkforward_v1.py` — P12 均线多头
  市场宽度保护；固定三候选、前一日状态、迟滞和历史拒绝门。
- `backend/scripts/tushare_sync.py` — 安全增量 Tushare 同步；旧分区跳过、缺口前一日
  复权因子衔接、新分区原子落盘、失败显式 manifest。
- `backend/research/regime/market_structure.py` /
  `run_market_structure_v1.py` — 全市场结构牛/结构熊因果标签、连续区间和运行时缓存。
- `backend/research/regime/run_structure_strategy_replay_v1.py` — P13 七配置四折同次对照；
  牛腿为趋势突破/均线多头，熊腿为现金/回踩，并保留全时段基线。
- `backend/research/validation/run_structure_strategy_forward_watch_v1.py` — P14 两个
  always-on 候选的注册后前向观察；固定400股、执行参数、t-1结构归因和60/120日门槛。
- `backend/research/optimization/run_structural_bull_challenge_v1.py` — 全量非 ST 的
  结构牛市高胜率/高收益样本内挑战；固定30候选并在既有强牛窗口做冻结泛化审计。
- `backend/research/regime/diag_f4_regime.py` — F4 regime 信号诊断（输出 `artifacts/current/diag_f4_regime.json`）。
- `backend/research/reporting/make_regime_ensemble_report.py` — 读两个 JSON 生成综合 HTML 报告（Chart.js）。

### 产物（最新、可信）
- `artifacts/current/regime_ensemble_report.html` — canonical universe 综合报告；明确撤回旧 flat_leader 结论 ✅
- `artifacts/current/strategy_regime_ensemble.json` — 8 配置 × 4 折 + universe hash + 2×2 归因 ✅
- `artifacts/current/diag_f4_regime.json` — F4 信号诊断（ew 0% 牛 / leader 88.5% 牛）✅
- `artifacts/archive/validation/strategy_composition_wf_v1.json` — 组合固定协议、30 次回测、历史/未见分层聚合与失败记录 ✅
- `artifacts/archive/optimization/core_strategy_walkforward_v1.json` — protocol v2 五个 core
  参数历史复验；全部拒绝，生产参数未写回 ✅
- `artifacts/archive/optimization/core_portfolio_walkforward_v1.json` — protocol v2 仓位
  结构历史复验；趋势突破相对改善但稳定性门失败，五个 core 全部拒绝 ✅
- `artifacts/archive/validation/core_strategy_forward_watch_v1.json` — 2026-07-01 起的
  冻结观察；当前15日、`PENDING_DATA`、无失败记录 ✅
- `artifacts/archive/optimization/core_exit_walkforward_v1.json` — 退出/风控历史复验；
  五个 core 全部拒绝、无未来观察候选、无生产写回 ✅
- `artifacts/archive/optimization/bullish_breadth_walkforward_v1.json` — P12 市场宽度
  保护历史复验；-23.34% 对默认 -0.90%，gate FAIL、默认关闭 ✅
- `artifacts/archive/regime/market_structure_v1.json` — P13 逐日标签、协议哈希、特征、
  连续区间和数据清单；前置行情已用于暖机，研究期无 warmup ✅
- `artifacts/archive/regime/structure_strategy_replay_v1.json` — P13 七配置四折历史复验；
  所有切换候选均失败，禁止生产启用 ✅
- `artifacts/archive/validation/structure_strategy_forward_watch_v1.json` — P14 冻结
  协议；注册日2026-07-29，fresh起点2026-07-30，当前0日 `PENDING_DATA` ✅
- `artifacts/archive/optimization/structural_bull_challenge_v1.json` — 60%/80% 挑战的
  完整候选、股票池哈希和外部窗口结果；`REJECTED_OVERFIT`，禁止生产写回 ✅
- `artifacts/archive/optimization/legacy_core_strategy_walkforward_v1_pre_no_signal_fix_20260726.json`
  — 修正“无信号折被排除”偏差前的审计备份，不得作为结论。
- `artifacts/archive/regime/legacy_nondeterministic_20260726/` — universe 缺陷修复前的三项 current 备份，仅供追溯，不得作为权威结论。
- `artifacts/archive/factors/legacy_nondeterministic_20260726/` — 旧 P7/P8/P9/B JSON 与
  旧 Markdown 报告隔离区；不得与 canonical JSON 混用。
- `artifacts/archive/factors/canonical_factor_replay_20260726.md` — canonical 因子链复算摘要 ✅
- `artifacts/archive/factors/strategy_factor_search_universe.json` — P7 完整 universe 清单与哈希 ✅
- `artifacts/archive/factors/strategy_factor_oos.json` / `strategy_factor_semantic.json` —
  canonical P8（seed 20260622/20260723、范围 2025-01~2026-06-24）✅
- `artifacts/archive/factors/strategy_factor_walkforward.json` / `strategy_factor_engine_wf.json` —
  canonical P9/B（hash `5e2a6b75...e62efd`）✅
- `artifacts/archive/factors/alphagpt_v1.json` — P10 配置、universe、折区间、完整候选谱系、
  奖励分项、失败记录、同预算结果和冻结后 HOLDOUT 报告
- `artifacts/archive/factors/alphagpt_rollouts_v1.jsonl` — P11-A 逐 token transition
- `artifacts/archive/factors/alphagpt_rollouts_v1_manifest.json` — 数据来源哈希、
  vocabulary、episode split、训练折指标和失败审计
- `artifacts/archive/factors/alphagpt_bc_v1.npz` — P11-B 确定性模型 checkpoint
- `artifacts/archive/factors/alphagpt_bc_v1.json` — validation、生成多样性和
  random/evolution/ngram/Transformer 同预算训练奖励
- `artifacts/archive/factors/alphagpt_reward_model_v1.npz` — P11-D 公式级 ridge
  checkpoint
- `artifacts/archive/factors/alphagpt_reward_model_v1.json` — train-only CV、
  validation 排序/校准和泄漏防线；Spearman +0.618，top-20% lift +5.940，gate PASS
- `artifacts/archive/factors/alphagpt_reward_reranker_v1.json` — 三个新 seed 的
  前瞻同预算对照；reranker -0.649 vs random -1.853，2/3 seed 胜出，但绝对奖励
  仍为负，gate FAIL
- `artifacts/archive/factors/alphagpt_reward_labels_v2.json` — P11-E 六个新
  随机 seed 的 236 个成功标签（train 158 / validation 78）和 4 个失败记录
- `artifacts/archive/factors/alphagpt_rank_model_v2.npz` /
  `alphagpt_rank_model_v2.json` — 训练 seed 留一 CV 选中 pairwise alpha=100；
  validation Spearman +0.072、top-20% lift -0.439，gate FAIL
- `artifacts/archive/factors/alphagpt_research_v1_manifest.json` — AlphaGPT
  Research v1.0 机器可读发布事实源
- `artifacts/archive/factors/alphagpt_research_v1_release.md` — 人读版本说明、
  统一命令和 15 个发布产物 SHA-256
- `artifacts/archive/factors/alphagpt_rollouts_multiseed_v1.jsonl` / manifest —
  4 个 evolution seed 的训练 rollout
- `artifacts/archive/factors/alphagpt_bc_stability_v1.json` — 3 模型 seed 稳定性与
  pre-PPO gate（当前 FAIL）
- `artifacts/archive/factors/alphagpt_reward_conditioned_stability_v1.json` —
  P11-C2 两种奖励条件化方法（均 FAIL）
- `artifacts/archive/factors/strategy_factor_walkforward.json` / `artifacts/archive/factors/strategy_factor_semantic.json` — P9 / P8 OOS 结果

---

## 3. 工作脉络与诚实结论（时间线）

1. **P8 语义因子 + 多重检验（canonical replay）**：6 个语义因子的 OOS Sharpe
   看似为正，但 Bonferroni 校正后全部 `FAIL`；这些区间已被后续研究看过，不能再称 fresh OOS。
2. **P9 多区间 walk-forward（canonical replay）**：`mom_trend` 4/4 折因子级
   Sharpe 为正，但仅 2/4 折跑赢随机 null，预注册稳健规则为 `FAIL`；其余 5 个语义因子也全部失败。
3. **B 方案真实引擎（canonical replay）**：`mom_trend` 1/4 正折、均值 -3.65%；
   `pullback_to_support` 2/4 正折、均值 -0.34%。因子级好看没有转化成可交易 alpha。
4. **regime 条件化**：
   - 第一版（有信号源混淆，已弃用）见 `artifacts/archive/regime/strategy_regime_conditional.json`。
   - 2026-07-26 修复 universe 确定性后的 canonical 复算：
     | 配置 | 均值收益 | Sharpe | MDD | 正折 |
     |---|---|---|---|---|
     | flat_leader（牛mom/熊空仓·leader） | -10.44% | -1.03 | -20.45% | 1/4 |
     | mom_trend（无regime） | -3.65% | -0.12 | -20.63% | 1/4 |
     | switch_leader（熊切pullback） | -8.63% | -0.61 | -23.76% | 1/4 |
     | switch_ew（熊切pullback·ew） | +0.27% | +0.21 | -16.73% | 3/4 |
     | ensemble（6因子等权） | -7.95% | -0.46 | -22.42% | 1/4 |
   - **结论**：没有配置达到可晋级标准。`switch_ew` 均值略正且 3/4 正折，但 F4
     -17.19%，并且 ew 信号长期卡熊，不能据此推广。F4 信号退化诊断仍成立。
5. **策略组合 v1 固定协议复验**：canonical universe hash
   `5e2a6b75...e62efd`，固定 AND/30% 基础评分/70% mom_trend，不做权重搜索。
   历史四折中，pullback+factor 相对基线均值收益 +4.37 个百分点；
   bullish+factor+flat 相对 flat_leader +6.70 个百分点。但 2026-07-01~07-21
   只有 15 个交易日，所有配置绝对收益均为负；这只是候选假设，不是新 OOS 晋级。
6. **P10 AlphaGPT 闭环 v1**：逐 token action mask 保证公式能收口并由既有 StackVM
   执行；候选按规范化哈希去重并做训练信号相关性裁剪；奖励综合训练折中位 ICIR、
   正收益折比例、稳定性及换手/复杂度/方差/相关性惩罚；random/evolution 使用相同
   evaluator 调用预算。HOLDOUT 不参与生成、筛选、调参、断点或 early stopping。
   默认运行使用训练期抽样 universe（请求 400、三折共同有效 393），两路均为 40/40
   次评估；`--resume` 前后产物 SHA-256 一致。验收：AlphaGPT 专项 16 passed（含
   10,000 公式），完整回归 416 passed，compileall 通过。
7. **P11-A policy adapter + offline rollouts**：不改 P10 搜索逻辑；把 random、
   teacher replay 和未来模型统一到 `TokenPolicy`，模型 logits 必须走中央 mask。
   当前产物包含 27 个 accepted evolution episode、237 个 transition，确定性拆为
   train 23 / validation 4，采集失败 0；P10 产物在采集前后哈希不变。验收：
   AlphaGPT 专项 25 passed，完整回归 425 passed，compileall 通过。
8. **P11-B masked behavior cloning**：本地无 torch/sklearn，使用纯 NumPy 单层单头
   Transformer，validation NLL/accuracy 为 1.265/63.9%，优于 n-gram
   1.439/61.1%；1,000 公式合法率 100%、唯一率 59.6%。同 40 次训练评估平均奖励：
   evolution +1.849 > Transformer +1.123 > random -0.191 > n-gram -2.078。
   模型仍有 12 次重复、9 个高相关拒绝，结论是“学到部分先验但未超过 evolution”。
   验收：AlphaGPT 专项 28 passed，完整回归 428 passed，compileall 通过。
9. **P11-C 前置多 seed 稳定性**：4 个 evolution seed 各 40 次训练评估，跨 seed
   去重后得到 136 episode / 1,017 transition（train 887 / validation 130）。
   三模型 seed 公式合法率均 100%、唯一率均值 77.1%，validation accuracy 标准差
   2.5%，但各 seed 平均训练奖励全部为负，总均值 -0.748；pre-PPO gate 为 FAIL。
   原因不是语法崩溃，而是普通 BC 模仿了包含低奖励候选的平均行为。验收：
   AlphaGPT 专项 34 passed，完整回归 434 passed，compileall 通过。
10. **P11-C2 reward-conditioned BC**：reward-weighted 使用全部 117 个训练 episode，
   elite 使用奖励前 40%（47 episode，奖励阈值 +0.331）。elite 将平均训练奖励从
   uniform -0.748 改善到 -0.234，但 3/3 seed 仍为负；reward-weighted 均值
   -0.938，仅 1/3 seed 为正。两种 gate 均 FAIL，禁止进入 PPO。验收：
   AlphaGPT 专项 37 passed，完整回归 437 passed，compileall 通过。

---

## 4. ⚠️ 关键坑 & 已修 bug（接手前必读，否则会重蹈）

### 🔴 BUG-1：`_leader_bull_map` 累计和 MA 写错（已修，勿重犯）
- **症状**：自实现 leader 信号全判熊（仅 0.2% 牛），导致 `switch_leader ≡ switch_ew`（两者都卡熊）的**假象**，掩盖了 2×2 真实差异。
- **根因**：手写 `c += level[i]` 但**从不减窗口首项** → MA 随 i 单调爆炸 → 永远判熊。
- **修复**：改用 `pl.Series(level).rolling_mean(ma_win)`（polars 在白名单内）。引擎本就用同款，给 **~70% 牛**。
- **教训**：复刻引擎 regime 信号时，MA **必须用 `rolling_mean`**，严禁手写累计和。

### 🟡 PITFALL-2：ew 等权信号 0% 牛是真实样本属性，非 bug
- 等权 400 只篮子在本区间（2025-01~2026-06）持续低于 MA60/MA20（横盘阴跌），而 leader 龙头指数涨 ~10% → **龙头 vs 等权明显背离**。引擎 `market.close` 不前向填充，自算指数需自己 ffill（已在 `_regime_bull_mask` 内逐标的前向填充后再取等权均值）。
- 不要因为 ew 信号 0% 牛就以为又有 bug——这是 F4 炸裂的根因本身。

### 🟡 PITFALL-3：未排序 universe 会让固定 seed 失效（已修复）
- 根因不是 seed，而是 Polars `unique()` 返回顺序不稳定；对其直接 `sample()` 会让独立进程得到不同股票池。
- **规则**：统一调用 `research.common.universe.stable_symbol_sample`；产物必须带完整 symbol
  manifest 和 SHA-256。跨运行只有在 universe hash、日期、配置均一致时才允许对照。

### 🟡 PITFALL-4：引擎暖机 120 根
- 见 §1。算 MA 窗口按 120 估；MA60 在熊偏样本会全判熊 → 牛腿塌缩（这是「MA60 的 2×2 退化」的来源，不是 bug）。

---

## 5. F4 炸裂根因（已坐实）

`switch_ew` 在 F4 亏 -17.19%。**不是 whipshaw，是 ew MA60 信号滞后**：
- 等权指数 F4 实际 **+5.60%**，但全程在 MA60 下方（均值 |偏离| 14.91%、最大 30.40%）→ **100% 判熊、0 翻转**。
- 策略整段只部署 pullback（均值回归）熊腿 → 逆着上涨市做反转而炸裂。
- 对照：leader 信号 F4 有 **88.5% 牛**、5 次翻转更灵敏，canonical universe 下 `switch_leader` F4 为 -3.88%。
- **启示**：问题在「ew 信号滞后」，不在「切换」这个动作。修法见 §7-③。

---

## 6. 当前可复用结论

1. **当前没有“最强策略”**。canonical universe 上 8 个 regime/ensemble 配置没有一个通过稳健晋级门槛。
2. **固定 seed 前必须先固定输入顺序**。对 `unique()` 的无序结果直接 `sample()` 会让每个进程抽到不同股票池；所有研究产物应记录 universe hash。
3. **组合机制可用，但 30/70 不是已验证最优权重**。pullback+factor 的历史相对改善在 15 日未见观察期没有形成明确绝对优势。
4. **F4 的 ew 信号退化结论仍成立**：等权指数上涨但长期位于 MA 下方，导致策略卡在错误腿；这不等于 switch_ew 已被证明有效。
5. **因子等权 ensemble 仍无证据**（高度共线）；若继续，应使用训练折 IC 加权/正交化并另留新时间段。

---

## 7. 接手后推荐路线

| # | 方向 | 落点 | 价值 |
|---|---|---|---|
| ✅ | **canonical universe 清单与 manifest**：共享排序采样、完整 symbol 列表、范围和哈希已落盘，受影响研究链已重跑 | `research/common/universe.py` | 已完成 |
| ① | **等待/积累真正新时间段**：OBS1 仅 15 个交易日，至少积累 60–120 个交易日再做一次冻结 gate | 当前 `PENDING_DATA`；复用 frozen protocol，不改旧权重 | 判断组合相对改善能否延续 |
| ② | **训练折内做组合权重选择**：只在 expanding train 中比较少量预注册权重，再锁定后跑未来 OBS | 扩展 `research/validation/run_strategy_composition_wf.py` | 合法调优组合，不复用旧测试折选权重 |
| ③ | **P11-F execution-aware 低成本代理**：停止继续堆 token 结构模型；只用固定 T1–T3 小型 calibration sketch 执行公式，提取信号方差、横截面离散度、时序自相关、换手代理和基础特征相关性，再做一次全新 seed gate | 新增独立 proxy-feature 模块；不能复用 P11-D/P11-E validation 调参 | 判断奖励不可学是结构特征不足，还是公式空间本身噪声过高 |

> AlphaGPT 路线的下一步是 ③；P11-E 说明结构 token 在随机公式分布上没有稳定
> 排序能力。P10/P11-A
> 产物已锁定为 CPU 与数据基线。策略路线当前只能等待 ①；在没有足够新数据前，
> 不要把旧四折继续切碎包装成新 OOS。

> `custom_factor` 对所有有限值资产都会给 entry，与筛选策略组合时默认使用 AND；
> OR 会把候选池扩成接近因子单跑。当前 30/70 只是预注册基准，不是最优权重。

---

## 8. 速查命令

### 选股逻辑 v1（2026-08-09）

已新增实验策略 `quality_momentum_v1` 和 `research/selection/` 审计链。它把旧均线多头的
“动量 + 高换手优先”替换为固定质量评分，并对追高、波动、回撤和跳空显式扣分；每只
非 ST 股票的信号门槛、排名和淘汰原因均写入 CSV。

全量非 ST、每窗口同预算历史诊断结果：

| 窗口 | 旧均线多头 | 质量动量 v1 | 结论 |
|---|---:|---:|---|
| 2025-05-24~08-24 | -10.33% | +35.35% | 改善，但已见历史 |
| 2025-07-24~10-24 | -31.02% | +2.74% | 改善，但已见历史 |
| 2026-03-24~06-24 | +18.40% | +1.18% | 在已污染目标窗相对退化 |
| 2026-06-25~08-07 | -53.44% | -32.70% | 仅少亏，熊段仍失败 |

截至 2026-08-07，5329 只非 ST 中有 56 只通过信号门槛；最新展示按 10 只持仓、当前
行业最多 2 只留下 10 只。行业分类不是 point-in-time，只用于最新展示；本地无历史
消息库，消息覆盖保持关闭。状态为 `HISTORICAL_REPLAY_ONLY`，生产默认未改，下一步只能
注册冻结日期并等待真正新数据。

2026-08-10 早盘已在上述 56 只基础候选上叠加 Tushare 9:25 集合竞价：56 只全部匹配，
19 只确认、36 只观察、1 只因追高拒绝，按基础排名与行业最多 2 只规则输出 10 只。
竞价只做固定规则确认，不重拟合基础评分；原始快照写入
`data/tushare_auction/date=2026-08-10/open.parquet`。该结果是 `LIVE_SCREEN_ONLY`，没有
9:30 后实时行情权限，不能把竞价价表述为当前价或成交承诺。

产物：`artifacts/archive/selection/selection_logic_v1.json`、
`artifacts/archive/selection/selection_logic_v1_latest_audit.csv`、
`artifacts/archive/selection/auction_selection_20260810.json`、
`artifacts/archive/selection/auction_selection_20260810.csv`。

同日新增独立的“一进二竞价 v1” specialized runner：前日仅取
`consecutive_limit_ups == 1` 的首板，竞价成交额占首板全天成交额 8%~12% 得满分，
12%~20% 线性降分，区间外淘汰；竞价涨幅 6%~8% 与 MA5>MA10>MA20 且三线同步向上
都是硬门槛；首板阳线穿越 MA5/10/20/60 至少两条加 20 分。2026-08-10 的 62 只首板
全部匹配竞价，但严格入选 0 只；最接近的海正药业竞价涨幅仅 5.80%，没有擅自放宽。
产物：`artifacts/archive/selection/first_board_second_day_20260810.json/csv`。该策略不进入
core 默认清单，历史竞价数据补齐并冻结协议前只能是 `LIVE_SCREEN_ONLY`。

### 仓库结构治理（2026-08-09）

- 根架构文档统一为 `ARCHITECTURE.md`，仓库内引用已更新。
- 13 个无明确对象名的旧研究脚本没有删除，已按 optimization/regime/validation/reporting
  移入 `backend/research/legacy/` 并改成描述性名称；旧模块路径保留为无业务逻辑的薄
  兼容入口，映射见 `backend/research/legacy/README.md`。
- factors、regime、optimization、validation、reporting、legacy 均有本域 README。
- 旧面板功能/策略手册移入 `docs/archive/legacy-panel/`；当前 `docs/strategy.md` 只描述
  无前端的后端策略开发。
- `backend/scripts/check_structure.py` 自动拒绝根目录 JSON/HTML/日志、顶层前端目录、
  `app -> research` 反向依赖、模糊活跃 runner 和未登记的 current 产物。
- 仓库名因 Git 远程与发布兼容保留 `tickflow-stock-panel`；这不授权恢复前端。

```bash
# 跑权威 regime 归因 + ensemble（8 配置 × 4 折）
cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.regime.run_regime_ensemble > ../artifacts/logs/regime_ensemble_run.log 2>&1

# 重跑 F4 诊断
cd backend && .venv/Scripts/python.exe -m research.regime.diag_f4_regime

# 重新生成综合报告 HTML（读取已落地的两个 JSON）
cd backend && .venv/Scripts/python.exe -m research.reporting.make_regime_ensemble_report

# 编译检查新建/改动文件
.venv/Scripts/python.exe -m py_compile app/strategy/builtin/regime_conditional.py app/strategy/builtin/factor_ensemble.py research/regime/run_regime_ensemble.py

# AlphaGPT v1（默认 random/evolution 各 40 次训练评估）
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_alphagpt_v1

# AlphaGPT 专项测试（含 10,000 条公式合法性）
cd backend && .venv/Scripts/python.exe -m pytest tests/research/alphagpt -q

# P11-A 离线 rollout
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_rollout_collection

# P11-B 纯 NumPy masked Transformer
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_behavior_clone

# P11-C 前置：多 seed 扩容与稳定性
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_rollout_expansion --resume
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_behavior_stability

# P11-C2：奖励条件化 BC
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_reward_conditioned_stability

# P11-D：公式 reward model 与前瞻 reranker
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_reward_model
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_reward_reranker

# P11-E：随机公式奖励标签与 pairwise/listwise ranker
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_reward_label_expansion
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_rank_model_v2

# AlphaGPT Research v1.0 发布/验收
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_release_v1
cd backend && .venv/Scripts/python.exe -m research.alphagpt.run_release_v1 --verify-only

# 五个 core 的 train-only 参数 walk-forward；已有同协议产物时可断点复用
cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.optimization.run_core_strategy_walkforward_v1 --resume
cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.optimization.run_core_portfolio_walkforward_v1 --resume
cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.validation.run_core_strategy_forward_watch_v1
cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.optimization.run_core_exit_walkforward_v1 --resume
cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.optimization.run_bullish_breadth_walkforward_v1 --resume

# Tushare 安全增量同步（凭据仅用环境变量传入；不会删除旧日线）
cd backend && .venv/Scripts/python.exe -m scripts.tushare_sync --start 20240401 --end 20260728 --index-start 20240401 --workers 8

# P13 结构标签与双腿历史复验
cd backend && .venv/Scripts/python.exe -m research.regime.run_market_structure_v1
cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.regime.run_structure_strategy_replay_v1

# P14 注册后冻结观察；运行前先增量同步并重算结构标签
cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.validation.run_structure_strategy_forward_watch_v1
cd backend && TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe -m research.selection.run_selection_logic_v1
cd backend && .venv/Scripts/python.exe -m scripts.check_structure
```

---

## 9. 给接手同事的提醒

- **先看 `artifacts/current/regime_ensemble_report.html`** 再读代码，结论和图表都在里面。
- **改任何 regime 信号相关代码前，先确认你用的是 `rolling_mean` 而不是累计和**——这是本项目踩过的最大坑。
- 跨运行对比前必须确认 universe hash、日期、配置和协议哈希完全一致；否则只允许同次运行内对照。
- 回测与研究只读取 canonical 本地 parquet；外部 Tushare 只能先经
  `scripts.tushare_sync` 校验落盘，禁止在回测循环里实时请求。日常补数不得删除旧日线。
- P11-D validation gate 通过不等于可接入搜索；前瞻绝对奖励 gate 已失败。下一位同事
  不要把 reranker 接到 evolution/PPO，也不要在这 19 条 validation 公式上继续调参。
- P11-E 的 train-seed CV 和新 validation 均不支持 token-only ranker。不要运行
  P11-E 前瞻测试或补扫 alpha/gap/pair 数；下一次实验必须换成 execution-aware
  特征并使用全新 validation seed。
- 项目级工作日志在 `.workbuddy/memory/2026-07-23.md`，含更细的分步记录。
- 2026-07-29 最终验收：`compileall` 通过；P13/P14 结构标签、历史复验和冻结观察后的完整 `pytest` 为
  `526 passed, 11 warnings`；AlphaGPT release v1.0 的 15 个产物校验通过。P14
  冻结协议专项与产物生成同样通过。
