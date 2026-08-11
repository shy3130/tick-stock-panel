# AGENTS.md

本文件是 AI 编码代理和自动化工具在 `tickflow-stock-panel` 仓库中的项目级入口。目标是让接手者先恢复正确上下文，再进行可复现、可比较的样本外（OOS）研究。

## 1. 开始任何任务前

按顺序读取：

1. `HANDOFF.md`：项目现状、文件地图、已验证结论、已修 bug 和下一步路线。
2. `ARCHITECTURE.md`：目录边界、依赖方向、命名和新增文件放置规则。
3. `.workbuddy/skills/regime-conditional-oos/SKILL.md`：涉及 regime 条件化、真实回测引擎、walk-forward、flat/switch 对照或 F4 诊断时的工具工作流。
4. 与任务直接相关的实现文件；不要只根据报告标题推断代码行为。

涉及当前 regime 结论时，再读取以下三个最新产物：

- `artifacts/current/regime_ensemble_report.html`：面向人的综合报告，先看图表和诚实结论。
- `artifacts/current/strategy_regime_ensemble.json`：8 配置 × 4 折结果及 2×2 归因的机器可读事实源。
- `artifacts/current/diag_f4_regime.json`：F4 regime 信号诊断的机器可读事实源。

旧产物只用于追溯，不得覆盖上述最新产物的结论。尤其不要把 `artifacts/archive/regime/strategy_regime_conditional.json` 或 `artifacts/archive/regime/regime_conditional_report.html` 当成当前权威结果。

## 2. 项目定位

这是 A 股量化策略因子研究与真实回测引擎项目。核心目标是用 walk-forward OOS 和多重检验判断 alpha 是否可靠，而不是优化出漂亮的样本内曲线。

当前最值得复用的是研究协议和执行能力，不是某个收益赢家：

- canonical universe 必须先排序再按 seed 抽样并记录完整 manifest/hash。
- 当前 regime、结构切换、因子 ensemble 和核心策略历史复验都没有通过稳健晋级门槛。
- `quality_momentum_v1` 已具备逐股解释能力，但仍是 mixed historical replay，默认隐藏。

不得把历史窗口相对改善表述为未来收益承诺。

## 3. 不可违反的研究约束

### 环境与数据

- Windows Python 固定使用 `backend/.venv/Scripts/python.exe`，不要使用全局 Python。
- 回测和研究只读取项目根目录 `data/kline_daily_enriched/**/*.parquet`；不要改成
  `backend/data/`。Tushare 外部数据只能先经 `backend/scripts/tushare_sync.py`
  校验并增量落入 canonical `data/`，禁止在回测或研究循环里实时请求外部 API。
- Tushare 日常补数默认跳过已有有效股票日线，绝不删除 `kline_daily/`；
  `--clear-first` 只用于用户明确授权的灾难恢复，不得当成普通更新命令。
- Windows 回测必须设置 `TICKFLOW_BACKTEST_MODE=inprocess`，避免进程 spawn 故障。
- 引擎暖机按 `max(120, warmup_bars * 1.6)` 估算；声明 60 根暖机时实际至少会使用 120 根。

PowerShell 权威运行示例：

```powershell
Set-Location backend
$env:TICKFLOW_BACKTEST_MODE = "inprocess"
.\.venv\Scripts\python.exe -m research.regime.run_regime_ensemble `
  *> ..\artifacts\logs\regime_ensemble_run.log
```

### OOS 与可比性

- 保持 `backend/research/factors/run_factor_engine_wf.py` 定义的统一口径：`N_SYM=400`、`SEED=20260723`、`FULL0=2024-09-24`、`FULL1=2026-06-30`、`N_FOLDS=4`、`TRAIN_SKIP_TD=80`。
- 结论必须来自同一次运行内的多配置可控对比。
- parquet 集合增长会导致固定随机种子下的抽样 universe 漂移；不得直接比较不同运行的绝对收益。
- 调参、因子选择和 regime 选择只能使用训练区间信息；测试折不得反向参与选择。
- 报告错误折、无信号折和空结果，不得静默删除或用 0 替代。

### Regime 实现

- 复刻 regime 移动平均时必须使用正确的 rolling mean；禁止用只累加、不移除窗口首项的累计和。
- 等权指数需要逐标的前向填充后再聚合，避免缺失值扭曲信号。
- `ew` 信号在现有样本中出现 0% 牛市是已确认的数据属性，不要未经证据把它改成 bug。
- 引擎原生 regime 是软减仓，不会自动更换策略；牛熊腿硬切换必须通过独立策略实现。

### Builtin 策略安全边界

- builtin 策略同样经过 `ai_generator._validate_safety`。
- 只允许导入 `polars`、`numpy`、`app.backtest.matrix`、`datetime`、`__future__`。
- 禁止 builtin 策略相互 import；需要的 DSL 或计算逻辑应自包含。
- `compute_signals(market, params)` 接收到的 market 含暖机 bars。
- 传给 `make_signal_matrix` 的 score 必须为 finite 的 `float32` 值。

## 4. 关键代码入口

- `backend/research/common/factor_dsl.py`：RPN 因子 DSL。
- `backend/research/alphagpt/environment.py`：AlphaGPT 合法 token 环境与 action mask。
- `backend/research/alphagpt/policy.py`：统一 TokenPolicy 与 masked logits 接口。
- `backend/research/alphagpt/run_rollout_collection.py`：只从 P10 训练候选生成 P11-A
  离线 rollout；不得读取最终候选 HOLDOUT 指标。
- `backend/research/alphagpt/behavior_clone.py`：P11-B 纯 NumPy masked Transformer。
- `backend/research/alphagpt/run_behavior_clone.py`：validation、生成审计和同预算
  训练奖励对照；当前模型弱于 evolution，扩容前不得直接升级到 PPO。
- `backend/research/alphagpt/run_rollout_expansion.py` / `run_behavior_stability.py`：
  P11-C 前置多 seed 扩容与稳定性 gate；历史 gate 为 FAIL。
- `backend/research/alphagpt/run_reward_conditioned_stability.py`：P11-C2 固定配置
  reward-weighted/elite BC；两种模式 gate 均为 FAIL。不得在相同 validation 上
  继续网格钓鱼。
- `backend/research/alphagpt/reward_model.py` / `run_reward_model.py`：P11-D 固定公式
  特征、训练内 ridge CV 与一次性 validation rank/top-k gate。
- `backend/research/alphagpt/reranker.py` / `run_reward_reranker.py`：P11-D 全新 seed
  候选池的前瞻同预算对照。当前相对随机有提升但绝对奖励仍为负，gate 为 FAIL；
  禁止接入 policy search 或 PPO。
- `backend/research/alphagpt/run_reward_label_expansion.py`：P11-E 随机合法公式
  的独立 seed 标签集，必须按完整 data seed 切分 train/validation。
- `backend/research/alphagpt/rank_model.py` / `run_rank_model_v2.py`：P11-E 纯 NumPy
  pairwise/listwise ranker。当前锁定 validation gate 为 FAIL；不得运行前瞻
  reranker、继续扫描参数或接入 PPO。
- `backend/research/alphagpt/run_release_v1.py`：AlphaGPT Research v1.0 唯一发布
  验证入口；冻结 15 个必需产物、跨文件哈希、数据边界和诚实 gate 状态。
- `backend/app/backtest/matrix.py`：信号矩阵契约与校验。
- `backend/app/backtest/strategy.py`：策略执行、暖机和 regime 接入。
- `backend/app/backtest/engine.py`：真实模拟循环和软减仓。
- `backend/app/strategy/builtin/custom_factor.py`：`mom_trend`。
- `backend/app/strategy/builtin/regime_conditional.py`：牛熊腿硬切换实现。
- `backend/app/strategy/builtin/factor_ensemble.py`：当前 6 因子等权 ensemble 反例。
- `backend/research/regime/run_regime_ensemble.py`：当前权威的 8 配置 × 4 折运行入口。
- `backend/research/regime/diag_f4_regime.py`：F4 信号诊断。
- `backend/scripts/tushare_sync.py`：安全 Tushare 增量同步、复权因子衔接和原子分区写入。
- `backend/research/regime/market_structure.py`：前一日全市场 breadth/收益生成结构牛熊标签。
- `backend/research/regime/run_market_structure_v1.py`：P13 标签事实源与可重建运行时缓存。
- `backend/research/regime/run_structure_strategy_replay_v1.py`：结构牛/熊双腿七配置四折复验。
- `backend/research/validation/run_structure_strategy_forward_watch_v1.py`：P14 注册后
  冻结观察；只有注册次日后的交易日可计入 fresh 门槛。
- `backend/research/reporting/make_regime_ensemble_report.py`：由 JSON 重新生成综合 HTML。

## 5. 修改原则

- 先写清假设、对照组、成功标准和验证方式，再改代码。
- 只修改任务需要的文件；保持现有风格，不顺手重构无关代码。
- 新实验应尽量在同一运行中加入基线和候选配置，保留干净归因。
- AlphaGPT 学习策略必须走 `TokenPolicy` 和现有 action mask；rollout、行为克隆和
  PPO 都不得把 P10 HOLDOUT 用作训练、验证、调参或 early stopping。
- 不覆盖历史产物来掩盖结果；新结果必须能追溯到运行脚本、参数、折区间和错误信息。
- 发现实现与 `HANDOFF.md` 或最新 JSON 不一致时，先以代码和可复现实验核实，再同步修正文档；不得静默选择对自己结论有利的一方。
- 不把回测结果写成“证明有效”；优先使用“当前 OOS 样本支持/不支持”。
- AlphaGPT Research v1.0 已是完整可交接版本。除非用户明确开始后续优化阶段，
  不要为了“看起来更完整”继续追加模型、PPO 或消费新 validation。

## 6. 最低验证要求

修改策略或运行脚本后，至少执行：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m scripts.check_structure
.\.venv\Scripts\python.exe -m py_compile `
  .\app\strategy\builtin\regime_conditional.py `
  .\app\strategy\builtin\factor_ensemble.py `
  .\research\regime\run_regime_ensemble.py
```

根据改动范围继续执行：

```powershell
$env:TICKFLOW_BACKTEST_MODE = "inprocess"
.\.venv\Scripts\python.exe -m research.regime.run_regime_ensemble
.\.venv\Scripts\python.exe -m research.regime.diag_f4_regime
.\.venv\Scripts\python.exe -m research.reporting.make_regime_ensemble_report
```

交付前确认：

- JSON 可正常解析，配置数、折数和聚合字段与脚本一致。
- HTML 报告读取的是最新两个 JSON，关键数值与 JSON 一致。
- 失败折和无信号折在 JSON 与报告中均可见。
- `git diff` 只包含本任务预期改动，且没有覆盖他人的未提交工作。
- `scripts.check_structure` 通过；不得新增根目录产物、前端目录、`app -> research`
  反向依赖或模糊命名的活跃研究入口。

## 7. 交接包维护

当最新结论、运行口径、关键 bug、权威脚本或推荐路线发生变化时，必须同步更新：

- `HANDOFF.md`
- `.workbuddy/skills/regime-conditional-oos/SKILL.md`（仅当工具工作流或硬约束变化）
- `artifacts/current/strategy_regime_ensemble.json`
- `artifacts/current/diag_f4_regime.json`
- `artifacts/current/regime_ensemble_report.html`

交付时简要说明改了什么、运行了什么验证、哪些结论仍受样本或数据限制。
