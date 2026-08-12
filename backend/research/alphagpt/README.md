# AlphaGPT v1 / P11-E

P10 的目标是先把“生成 → 训练折评估 → 去重/正交化 → 稳健奖励 → 选择 →
封存集报告”闭成一个可审计、CPU-only 的环，再接 Transformer/PPO。

## 模块边界

- `environment.py`：逐 token RPN 环境。动作掩码同时约束 StackVM 栈深、最大
  token 数、复杂度和剩余预算可收口性；`<STOP>` 仅在栈深为 1 时合法。
- `pool.py`：规范化 SHA-256 去重、谱系、训练折指标、失败原因和相关性裁剪。
- `reward.py`：只接受 `TrainingFoldMetrics` 的稳健奖励。
- `evolution.py`：mutation、语法树 crossover、elite selection、同预算随机基线和
  带 RNG 状态的断点文件。
- `run_alphagpt_v1.py`：固定 universe/seed/四块时间切分，T1-T3 搜索，最后一块
  HOLDOUT 仅在排名冻结后读取和报告。
- `policy.py`：统一 `TokenPolicy`、随机/教师重放策略和未来模型使用的
  `MaskedLogitPolicy`；所有动作都经过环境 mask。
- `rollouts.py`：逐步记录 tokens、token ids、栈深、复杂度、action mask、动作和
  最终训练奖励。
- `dataset.py`：确定性 episode 切分、JSONL transition 与 manifest。
- `run_rollout_collection.py`：只重放 P10 evolution 候选池中的 accepted 训练记录。
- `behavior_clone.py`：纯 NumPy n-gram 与单层单头 masked Transformer、Adam、
  validation-NLL early stopping 和确定性 checkpoint。
- `run_behavior_clone.py`：P11-B 训练、生成审计及四策略同 40 次训练评估对照。
- `run_rollout_expansion.py`：固定 T1–T3 的多 evolution seed 扩容、跨 seed 去重
  和可续跑 checkpoint。
- `run_behavior_stability.py`：多模型 seed validation、生成多样性、训练奖励与
  pre-PPO gate。
- `run_reward_conditioned_stability.py`：reward-weighted 与 top-40% elite BC，
  使用相同三模型 seed gate。
- `reward_model.py`：固定公式结构特征、纯 NumPy dual ridge、train-only CV、
  Spearman/top-k/calibration 指标和确定性 checkpoint。
- `run_reward_model.py`：P11-D 训练与一次性 validation gate。
- `reranker.py`：未见公式 slate 生成、冻结模型评分和同 slate 随机选择。
- `run_reward_reranker.py`：三个新 seed 的前瞻同预算 T1–T3 对照。
- `reward_labels.py` / `run_reward_label_expansion.py`：P11-E 随机合法公式标签，
  完整 seed 切分，以及 intrinsic/operational reward 分离。
- `rank_model.py` / `run_rank_model_v2.py`：纯 NumPy pairwise/listwise ridge、
  train-seed 留一 CV 和锁定 validation gate。

现有实现必须复用：

- `research.common.factor_dsl.StackVM`
- `research.factors.run_factor_search.build_features`
- `research.factors.run_factor_search.cross_sectional_score`
- `research.paths` 中的产物目录

## 奖励

```text
1.00 * median(train ICIR)
+ 0.50 * positive train-return fold ratio
+ 0.25 / (1 + std(train ICIR))
- 0.10 * median(train turnover)
- 0.20 * normalized formula complexity
- 0.25 * variance(train ICIR)
- 0.25 * max absolute train factor correlation
```

每个正项、惩罚项、权重和公式原文都写入候选 JSON。HOLDOUT 的结构不能构造
`TrainingFoldMetrics`，`RobustReward.score` 也没有 test/holdout 参数。

## 运行

从 `backend/` 执行：

```powershell
.\.venv\Scripts\python.exe -m research.alphagpt.run_alphagpt_v1
```

默认每路评估 40 个唯一候选。断点位于
`artifacts/logs/alphagpt_v1/`；续跑时必须使用完全相同的配置：

```powershell
.\.venv\Scripts\python.exe -m research.alphagpt.run_alphagpt_v1 --resume
```

权威 P10 机器产物写入：

```text
artifacts/archive/factors/alphagpt_v1.json
```

P11-A 离线数据写入：

```text
artifacts/archive/factors/alphagpt_rollouts_v1.jsonl
artifacts/archive/factors/alphagpt_rollouts_v1_manifest.json
```

采集命令：

```powershell
.\.venv\Scripts\python.exe -m research.alphagpt.run_rollout_collection
.\.venv\Scripts\python.exe -m research.alphagpt.run_behavior_clone
```

数据集不会读取 `final_candidates` 或其中的 HOLDOUT 报告。教师轨迹来自
`searches.evolution.pool.candidates`，只保留 `status=accepted` 的训练候选；每条
transition 的 `final_training_reward` 来自 P10 已落盘奖励。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/research/alphagpt -q
.\.venv\Scripts\python.exe -m compileall -q research/alphagpt
```

P11-B 产物：

```text
artifacts/archive/factors/alphagpt_bc_v1.npz
artifacts/archive/factors/alphagpt_bc_v1.json
```

当前结果：Transformer validation NLL 1.265 / accuracy 63.9%，优于 n-gram 的
1.439 / 61.1%；1,000 公式合法率 100%、唯一率 59.6%。同 40 次训练评估中，
Transformer 平均奖励 +1.123，高于 random -0.191 和 n-gram -2.078，但低于
evolution +1.849。

## P11-B 完成边界与下一步

当前 Transformer 是纯 NumPy 的最小可验证模型，不需要 GPU/PyTorch，也没有 PPO
或在线策略部署。数据只有 201 条训练 transition，且模型生成出现 12 次重复、9 个
高相关拒绝；因此下一步应先做训练期多 seed rollout 扩容和多 seed 模型稳定性，
不要直接上 PPO。P10 HOLDOUT 已经消费，不能用于模型调参或 early stopping。

## P11-C 前置稳定性结果

```powershell
.\.venv\Scripts\python.exe -m research.alphagpt.run_rollout_expansion
.\.venv\Scripts\python.exe -m research.alphagpt.run_behavior_stability
```

四个 evolution seed 各使用 40 次训练评估，跨 seed 去重后共有 136 episode /
1,017 transition（train 887 / validation 130）。三个模型 seed 的公式合法率均为
100%，唯一率均值 77.1%，但各 seed 平均训练奖励全部为负，总均值 -0.748，
pre-PPO gate 因此失败。

扩容数据包含较多低奖励 accepted 候选，普通 behavior cloning 学到的是“平均
evolution 行为”，不是 elite 偏好。下一步应做 reward-weighted 或 elite-filtered
behavior cloning，并保持相同多 seed gate；当前禁止直接进入 PPO。

P11-C2 已执行上述两种方法：elite BC 将跨 seed 平均奖励从 uniform 的 -0.748
改善到 -0.234，但三个 seed 仍全部为负；reward-weighted BC 均值 -0.938，仅
1/3 seed 为正。两种模式均未通过 gate。不要继续在同一 validation 上扫描权重或
quantile；因此 P11-D 转向公式级 reward model/reranker，并以 rank correlation、
top-k lift 和新 seed 前瞻对照作为固定 gate。

## P11-D 公式 reward model 与前瞻 reranker

```powershell
.\.venv\Scripts\python.exe -m research.alphagpt.run_reward_model
.\.venv\Scripts\python.exe -m research.alphagpt.run_reward_reranker
```

模型只读取 117 条 train 公式的 token 与 T1–T3 `final_reward`。558 维特征由长度、
复杂度、栈深、元数比例、unigram/bigram 和首尾 token 组成，不读取行情特征；
ridge alpha 只在 train 内按公式哈希做 4 折 CV。19 条 validation 公式只用于一次
锁定后的排序、top-k 和校准 gate，公式 token/哈希一致性及跨 split 唯一性会强制
校验。

封存 validation 结果为 Spearman +0.618，bootstrap 5% 分位 +0.246，top-20% 实际
训练奖励均值 +7.765、相对全体 lift +5.940，因此允许执行一次前瞻 reranker。
前瞻测试使用三个新 seed；每个 seed 先生成 200 个未见合法公式，模型与随机基线从
完全相同的 slate 各选 20 个并各消耗 20 次真实 T1–T3 评估。reranker 平均
-0.649，随机 -1.853，paired improvement +1.204，reranker 在 2/3 seed 胜出；
但 reranker 绝对均值仍为负，最终 gate 为 FAIL。

产物：

```text
artifacts/archive/factors/alphagpt_reward_model_v1.npz
artifacts/archive/factors/alphagpt_reward_model_v1.json
artifacts/archive/factors/alphagpt_reward_reranker_v1.json
```

P11-D 的结论是“公式结构存在可学习的相对排序信号，但不足以产生正的前瞻训练
奖励”。不得把当前 reranker 接入 evolution、token policy 或 PPO，也不得继续在
这 19 条 validation 公式上调参。因此 P11-E 改用不重叠的随机公式 seed 标签，
锁定 pairwise/listwise 配置后再做新的 validation gate。

## P11-E 随机公式标签与 pairwise/listwise ranker

```powershell
.\.venv\Scripts\python.exe -m research.alphagpt.run_reward_label_expansion
.\.venv\Scripts\python.exe -m research.alphagpt.run_rank_model_v2
```

P11-E 使用 4 个 train seed 和 2 个 validation seed，每个 seed 预算 40，公式来自
与前瞻 slate 一致的 `AlphaEnv` 随机合法采样器。旧 rollout 和 P11-D 已评估公式
全部排除。最终得到 236 个成功标签（train 158 / validation 78）及 4 个显式失败。
切分单位是完整 data seed，禁止把同一 seed 的公式散切到两侧。

模型目标固定为 intrinsic reward，即 T1–T3 `RobustReward` 但相关性惩罚设为零；
依赖候选池顺序的 operational reward 仍完整落盘，仅用于审计。训练内以 4 个 seed
做 leave-one-seed-out CV，同时比较 pairwise/listwise 与 alpha
`{0.1,1,10,100}`，选中 pairwise alpha=100。

锁定 validation 结果：

- Spearman：+0.072
- pairwise accuracy：52.1%
- top-20% 实际 intrinsic reward：-1.256
- top-20% lift：-0.439
- bootstrap Spearman 5% 分位：-0.113

gate 为 FAIL，因此没有运行 P11-E 前瞻 reranker，也没有接入搜索/PPO。产物：

```text
artifacts/archive/factors/alphagpt_reward_labels_v2.json
artifacts/archive/factors/alphagpt_rank_model_v2.npz
artifacts/archive/factors/alphagpt_rank_model_v2.json
```

当前证据说明 unigram/bigram/栈结构等 token-only 特征无法在随机公式分布上稳定预测
奖励。若继续，下一阶段应使用固定 T1–T3 calibration sketch 提取低成本
execution-aware 特征，并使用全新 validation seed；不得继续扫描 P11-E 的
alpha、reward gap 或 pair 数。

## AlphaGPT Research v1.0 发布

P10–P11-E 已冻结为完整研究版本。发布入口不会重跑实验，只校验 15 个必需产物、
SHA-256、训练折边界、checkpoint 和 gate：

```powershell
.\.venv\Scripts\python.exe -m research.alphagpt.run_release_v1
.\.venv\Scripts\python.exe -m research.alphagpt.run_release_v1 --verify-only
```

发布产物：

```text
artifacts/archive/factors/alphagpt_research_v1_manifest.json
artifacts/archive/factors/alphagpt_research_v1_release.md
```

本版本的准确状态是：`research_version_complete=true`、
`production_alpha_ready=false`、`ppo_ready=false`、`frontend_included=false`。
后续优化另开 P11-F，不属于 v1.0 完整性修补。
