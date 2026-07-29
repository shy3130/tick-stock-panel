# Research

这里存放可复现的量化研究代码，不属于 FastAPI 生产应用。

## 目录

- `common/`：因子 DSL、市场代理等共享计算模块。
- `factors/`：因子搜索、语义因子、多重检验和因子级/引擎级 OOS。
- `alphagpt/`：P10 CPU-only 搜索闭环、P11-A rollout、P11-B 纯 NumPy masked
  Transformer、P11-C 多 seed 稳定性、P11-D reward model/reranker，以及 P11-E
  随机公式标签和 pairwise/listwise ranker。
- `regime/`：市场状态过滤、条件切换和 F4 诊断。
- `optimization/`：历史参数搜索实验；不能作为 OOS 证据。
- `validation/`：区间复验和 walk-forward 基准脚本。
- `reporting/`：从机器可读结果生成报告。
- `paths.py`：数据与产物的唯一稳定路径定义。

## 运行约定

从 `backend/` 目录以模块方式运行，避免依赖脚本所在目录：

```powershell
Set-Location backend
$env:TICKFLOW_BACKTEST_MODE = "inprocess"
.\.venv\Scripts\python.exe -m research.regime.run_regime_ensemble
.\.venv\Scripts\python.exe -m research.regime.diag_f4_regime
.\.venv\Scripts\python.exe -m research.reporting.make_regime_ensemble_report
.\.venv\Scripts\python.exe -m research.alphagpt.run_alphagpt_v1
.\.venv\Scripts\python.exe -m research.alphagpt.run_rollout_collection
.\.venv\Scripts\python.exe -m research.alphagpt.run_behavior_clone
.\.venv\Scripts\python.exe -m research.alphagpt.run_rollout_expansion
.\.venv\Scripts\python.exe -m research.alphagpt.run_behavior_stability
.\.venv\Scripts\python.exe -m research.alphagpt.run_reward_conditioned_stability
.\.venv\Scripts\python.exe -m research.alphagpt.run_reward_model
.\.venv\Scripts\python.exe -m research.alphagpt.run_reward_reranker
.\.venv\Scripts\python.exe -m research.alphagpt.run_reward_label_expansion
.\.venv\Scripts\python.exe -m research.alphagpt.run_rank_model_v2
.\.venv\Scripts\python.exe -m research.alphagpt.run_release_v1 --verify-only
.\.venv\Scripts\python.exe -m research.optimization.run_core_strategy_walkforward_v1
.\.venv\Scripts\python.exe -m research.optimization.run_core_portfolio_walkforward_v1
.\.venv\Scripts\python.exe -m research.validation.run_core_strategy_forward_watch_v1
.\.venv\Scripts\python.exe -m research.optimization.run_core_exit_walkforward_v1
.\.venv\Scripts\python.exe -m research.optimization.run_bullish_breadth_walkforward_v1
.\.venv\Scripts\python.exe -m research.regime.run_market_structure_v1
.\.venv\Scripts\python.exe -m research.regime.run_structure_strategy_replay_v1
```

数据固定读取项目根的 `data/`；产物固定写入项目根的 `artifacts/`。
生产代码 `app/` 不得 import `research/`。

## 五个核心策略参数复验

`research.optimization.run_core_strategy_walkforward_v1` 对 5 个 core 使用同一 canonical
400 股 universe、seed `20260723`、7 个 180/60/60 日 walk-forward 折和固定 Calmar
训练目标。每折参数只由训练段选择；测试段只评估。候选与默认参数使用相同回测配置，
无信号按持币 0% 明确计入。结果写入
`artifacts/archive/optimization/core_strategy_walkforward_v1.json`，绝不自动覆盖生产参数。

2026-07-26 的 protocol v2 历史复验中，五个策略均为
`REJECTED_HISTORICAL_REPLAY`。其中超卖反转候选累计 +24.76%，但正收益折和战胜默认折
都只有 4/7，未达到预注册的 60% 门槛；不能称为已优化策略，只能将
`rsi_max=40, min_change=2` 冻结为未来新数据候选。

仓位结构入口 `research.optimization.run_core_portfolio_walkforward_v1` 保持五个策略的
信号、参数、费用与执行规则不变，只在训练折比较 7 个固定持仓数量/分配候选。protocol
v2 的严格 JSON 产物为 `artifacts/archive/optimization/core_portfolio_walkforward_v1.json`。
趋势突破的训练选择累计测试收益从默认 -12.53% 改善到 +0.75%，但仅 4/7 正折、3/7
战胜默认，且训练赢家不稳定；五个策略最终仍全部拒绝，没有生产配置写回。

冻结前向观察入口为 `research.validation.run_core_strategy_forward_watch_v1`。它固定
2026-06-30 为校准截止日，从 2026-07-01 起只追加观察数据，协议改变时拒绝覆盖。
当前截至 2026-07-21 共 15 个交易日：超卖候选 -10.19% 对默认 -0.03%；趋势突破
20 只等权 -2.30% 对默认 10 只等权 -4.60%。趋势候选相对少亏但仍为负，状态为
`PENDING_DATA`，距最低 60 日还差 45 日。产物是
`artifacts/archive/validation/core_strategy_forward_watch_v1.json`。

退出与风控搜索入口为 `research.optimization.run_core_exit_walkforward_v1`，固定 7 个
候选并复用生产回测执行。均线多头历史累计从默认 -0.90% 改善到 +6.76%，但正折和
战胜默认均只有 4/7（门槛至少 5/7）；涨停动量从 -33.09% 改善到 -3.68%，仍为负。
五个 core 全部 `REJECTED_HISTORICAL_REPLAY`，因此没有建立 2026-07-22 后的新观察
候选。严格 JSON 产物为 `artifacts/archive/optimization/core_exit_walkforward_v1.json`。

P12 市场宽度保护复用生产 `regime_filter` 的软减仓通道，状态只读取前一交易日的
MA20/MA60 宽度并带迟滞；默认关闭。固定的默认/温和减仓/弱市空仓三候选历史复验中，
训练选择累计 -23.34%，默认 -0.90%，仅2/7正折、3/7战胜默认，因此 gate FAIL，
没有未来观察候选，也不再扫描阈值。产物为
`artifacts/archive/optimization/bullish_breadth_walkforward_v1.json`。

## P13 结构牛市 / 结构熊市

`research.regime.market_structure` 不把“2024-09-24 是牛市起点”直接写成每日标签。
它用前一交易日全市场 breadth(MA20/MA60)、20 日等权复合收益和两日确认区分
`structural_bull` / `structural_bear`；中间带保持原状态，避免频繁切换。等权路径
先算个股收益再截面平均，修正旧实验直接平均股票价格水平的概念错误。

当前本地 Tushare 日线有 3,039,038 行、5,628 只股票、562 个交易日，覆盖
2024-04-01~2026-07-27，唯一键无重复；enriched 与原始日线行数一致。同步默认
跳过旧分区，未删除旧日线。2026-07-28 的股票日线和 daily_basic 返回空数据，
已写入 `artifacts/archive/data/tushare_sync_latest.json` 的失败列表。

结构特征从 2024-04-01 起暖机，标签只输出 2024-09-24 之后：结构牛 191 日、
结构熊 253 日、无 warmup、20 次切换；最新 2026-07-27 是结构熊，使用的是前一
交易日 2026-07-24 信号。事实源：
`artifacts/archive/regime/market_structure_v1.json`。

`run_structure_strategy_replay_v1` 在 canonical 400 universe 的同一四折中比较：

- 趋势突破全时段复合 +11.88%，结构熊现金 -3.27%，结构熊回踩 -8.32%；
- 均线多头全时段 -12.27%，结构熊现金 -30.65%，结构熊回踩 -32.82%；
- 回踩支撑全时段 +31.29%，但这是历史四折描述，不能跨协议替代其他七折结论。

四个切换候选均只在 1/4 折战胜相应基线，状态为
`REJECTED_HISTORICAL_REPLAY`。切换能力保留，生产默认关闭，不得继续用这四折扫描
结构阈值或反向交换牛熊腿。

Tushare 安全增量命令：

```powershell
$env:TUSHARE_TOKEN = "<有效 token>"
.\.venv\Scripts\python.exe -m scripts.tushare_sync `
  --start 20240401 --end 20260728 --index-start 20240401 --workers 8
```

已有有效股票日线默认跳过；命令不会删除 `kline_daily/`。并发模式只并发请求，
规范化和原子落盘仍按交易日顺序执行。

注意：上述参数、仓位、退出脚本的“五策略”是已冻结的历史研究协议，不等于当前产品
目录。当前默认 core 已收敛为均线多头、趋势突破、回踩支撑三个；超卖反转与涨停动量
保留显式兼容但归类为 experimental。

## Universe 确定性

2026-07-26 审计发现，旧因子/regime 脚本曾对 Polars `unique()` 的未排序结果直接
调用 `random.Random(seed).sample()`；三个独立进程产生三个不同 universe 哈希。
相关旧收益产物已降级为 legacy。所有 seeded universe 必须：

1. 先按 symbol 稳定排序；
2. 再用本地 `random.Random(seed)` 采样；
3. 在产物记录选择方法、日期范围、universe size、seed、完整 symbol 列表和 SHA-256。

统一实现位于 `research.common.universe`。P7 使用 sidecar
`strategy_factor_search_universe.json`；P8/P9/B/regime/组合产物内嵌 manifest。

canonical 400 只 universe（seed `20260723`，范围截至 `2026-06-30`）SHA-256 为
`5e2a6b75dcfb4d617d55c8fbbfda6480ca608d05f2b44bed364452fd47e62efd`。
组合验证入口：

```powershell
.\.venv\Scripts\python.exe -m research.validation.run_strategy_composition_wf --resume
```

其 F1–F4 是已被早期因子选择看过的历史复验，不是 fresh OOS；OBS1
（2026-07-01~2026-07-21）虽未见但只有 15 个交易日。产物中的晋级门为
`PENDING_DATA`：最低 60 日、目标 120 日、还差 45 日；满足长度也只进入冻结复核，
不会自动晋级。

AlphaGPT v1 的搜索只读取前三个固定训练块；最后一块 HOLDOUT 在两路搜索排名
冻结后才加载。产物为 `artifacts/archive/factors/alphagpt_v1.json`，断点写入
`artifacts/logs/alphagpt_v1/`。P11-A rollout 只读取 evolution 候选池的训练记录，
输出 `alphagpt_rollouts_v1.jsonl` 和 manifest。后续 Transformer/PPO 只能替换
token policy，必须继续复用 action mask、StackVM、候选池、训练奖励和封存集边界。
P11-B 模型与报告为 `alphagpt_bc_v1.npz/json`。P11-C 和 P11-C2 的 pre-PPO gate
均为 FAIL。P11-D 公式 ridge 在 19 条封存 validation 公式上得到 Spearman +0.618
和 top-20% lift +5.940；但三个新 seed 的前瞻 reranker 虽优于随机
（-0.649 vs -1.853，2/3 seed 胜出），绝对训练奖励仍为负，最终 gate 为 FAIL。
不得接入 policy search 或 PPO。P11-E 进一步采集 236 个随机公式标签并按完整 seed
切分；train-seed CV 选中 pairwise alpha=100，但新 validation Spearman 仅 +0.072、
top-20% lift -0.439，gate 为 FAIL，因此未运行前瞻 reranker。下一步如继续应改用
固定训练 calibration sketch 的 execution-aware 代理特征，而不是再调整 token
结构模型。

AlphaGPT Research v1.0 已冻结为完整研究基线。统一验收命令：

```powershell
.\.venv\Scripts\python.exe -m research.alphagpt.run_release_v1 --verify-only
```

该命令校验 15 个必需产物及其哈希、rollout 数据集、checkpoint、训练折边界、
seed 切分和历史 gate；它不会重新训练，也不会把失败结果改写成成功结论。
