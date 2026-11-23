# 自动策略实验室设计（挖掘 → 组合 → 持续验证闭环）

状态：**P1（策略守护）、P2a（稳健选冠）、P2b（权重精调）、P2d（策略级组合）、P3（闭环编排）均已实施并通过测试与真实数据 E2E 验证**；P2c（overlay 候选）按 §4.3 约定"按需启动，默认不做"暂不实施（需扩展可信发布契约，本轮收益/风险比不划算）。本文回答一个需求：**自动挖掘并验证各种因子，组合成好用的策略，并持续组合、持续验证**。

落地口径与设计稿的差异（均已在代码与测试中体现）：

- **P2b**：权重网格固定首个因子为 1.0（截面分位打分天然尺度不变），实际枚举 `{0.5, 1.0, 1.5, 2.0}^(n-1)`（≤64 变体）；stage1 复用 run 已备矩阵跑外层测试窗口，stage2 全窗口复验退化 >20% 放弃；精调预算 = run 剩余额度（上限 96），预算不足自动跳过（4 因子冠军在 balanced 档默认跳过属预期）。精调结果作为**新候选行**追加（独立签名），不覆盖原冠军。
- **P2d**：相关性采用**日收益 Pearson**（≥30 个共同交易日，样本更多）；建议对相关性阈值 ≤0.70；生成 composite 需人工在前端确认（fail-closed：不在最新建议中的对直接拒绝），幂等（blake2b 签名），自动以 `trailing_reference` 基线入护。
- **P3**：`lab_schedule.run_scheduled_lab` 为调度单入口（周度挖掘 + 日度守护，失败互不阻断）；名额满时新冠军停在 pending 候选等位；retired 不自动删文件，腾名额走人工归档/删除既有路径。

结论先行：仓库已存在"因子池统计筛选 → 束搜索组合 → 嵌套样本外验证 → 晋升门槛 → 一键发布策略"的完整主链路（mining-v2，见 `docs/mining.md`）。本设计不重建挖掘引擎，而是补三块真正的缺口：

1. **P1 策略守护**：发布后的策略在新数据上持续复验，退化自动告警/标记退役——"持续验证"目前完全不存在。
2. **P2 组合进化**：冠军选择从"单点 OOS 均值"升级为稳健分（median+worst），并对冠军组合做权重精调（复用稳健优化器）——"组合"目前只有 1.0/2.0 离散权重的束搜索。
3. **P3 闭环编排**：把周度挖掘与每日守护合并为一个有名额上限、有战报的总控状态机——"持续组合并验证"的自动循环。

---

## 一、现状盘点（均为已实现，设计以此为基础）

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| 因子池统计筛选 | `backend/app/services/auto_mining.py` `screen_all_factors()` | 按 \|IC\|×\|IR\| 选因子池 |
| 束搜索组合 | `backend/app/backtest/mining.py` `beam_search_factor_combinations()` (L610) | 组合 ≤4 因子，权重只试全 1.0 或单因子 2.0 |
| 嵌套样本外验证 | `mining.py` `generate_nested_folds()` (L752) | 外层 504/126/63，内层 252/63/63，purge 30 + embargo 5 |
| 真实回测评估 | `backend/app/backtest/mining_runtime.py` `MatcherCandidateEvaluator` (L142) | 候选转 `factor_rank_research` 策略真跑回测，score=外层 OOS Sharpe |
| 晋升门槛 | `mining.py` `evaluate_candidate_gate()` (L198) | 折数/正收益比/Sharpe≥0.5/回撤≥-25%/交易数≥60/confidence |
| 发布成策略 | `backend/app/services/mining_candidates.py` `publish()` (L112) | 渲染 `mined_factor_*.py`，**不记录发布基线** |
| 周度定时挖掘 | `backend/app/services/mining_schedule.py` `run_weekly_mining()` (L126) | 数据指纹 + claim 防重 |
| 进程隔离与预算 | `mining_runtime.py` + `worker.py` + `MiningRunStore` | spawn 子进程，峰值 RSS ≤1.5GiB（实测 ~1.35GiB/41s） |
| 稳健优化基建 | `robust_optimizer.py` / `walkforward.py` / `guards.py` | `robust_score`=0.6×median+0.4×worst、K 段切分、目标白名单 |
| 评估器注入点 | `mining.py` `CandidateEvaluator` Protocol (L426) | `MiningService.run(..., evaluator=)` 可整体替换评估语义 |

**缺口**（本设计要解决的）：

- 发布即终点：`publish()` 之后策略与普通策略无异，因子失效无人知晓。
- 选冠不稳健：外层折 Sharpe 取均值，单折运气可登顶；与稳健优化器的 median+worst 语义割裂。
- 组合空间窄：权重 {1.0, 2.0}、无 overlay/风控维度、无策略级组合（composite 已有引擎但无自动流程）。

---

## 二、总体结构

```text
                ┌─────────────── P3 总控（名额上限 + 战报）───────────────┐
                │                                                        │
  周度:  screen_all_factors ──> MiningService 束搜索 ──> gate ──> publish ──┐
  (已有)  (因子池筛选)          (P2a: 稳健选冠)      (P2b: 权重精调)        │
                                                                          v
  每日:                                              策略守护档案 (基线 + 复验历史)
  (P1 新建)                                           healthy / watch / retired
                                                          │
                                            退化 > 阈值 ──┘──> 告警 + 标记退役(腾名额)
```

数据流纪律（关键）：**守护层（发布后数据）绝不反馈进取参加权**。P2 的精调只用挖掘窗口内的 K 段锚定；发布后数据只用于验证与退役判定。这是逐层 OOS：挖掘嵌套验证 → 晋升门槛 → 发布后纯 OOS 追踪。

---

## 三、P1 策略守护（持续验证）

### 3.1 数据模型

- 守护档案：`data/user_data/strategy_guardian/{strategy_id}.json`，每策略一份：
  - `baseline`：发布/入护时的指标快照（objective 分、max_drawdown、n_trades、valid_folds、`data_as_of`、definition 指纹）。
  - `baseline_kind`：`mining_gate`（发布时来自晋升门槛，天然 OOS）或 `trailing_reference`（手动入护策略，取入护日前的尾随窗口作参考——**不是 OOS**，仅作退化比较基准）。
  - `checks[]`：历次复验记录（≤52 条滚动），每条含窗口、各段分数、robust 分、退化百分比、判定。
  - `status`：`healthy | watch | retired`，及进入当前状态的时间与原因。
- 复验 run 工件：`data/research/guardian/runs/{run_id}/`，沿用 `MiningRunStore` 的 manifest/summary/events 模式（重启可恢复、SSE 可续传）。

### 3.2 基线写入

- `MiningCandidateService.publish()` 渲染 `mined_factor_*` 成功后，同步写守护档案（baseline 来自 gate 指标，`baseline_kind=mining_gate`）。失败不影响发布本身（守护是旁路，fail-open 于发布、fail-closed 于判定）。
- 手动入护：`POST /api/backtest/guardian/{strategy_id}/adopt`，任意策略可入护；首次先算尾随参考基线，标记 `trailing_reference`。

### 3.3 复验算法（每策略每次）

1. 窗口：`baseline.data_as_of` 之后的全部新交易日，用 `robust_optimizer.split_validation_windows` 切 K 段（K=2..4，每段 ≥14 交易日）；新增数据 <30 交易日则本轮跳过（记录 skipped）。
2. 每段一次真实回测：`StrategyBacktestService.run` + `BacktestResultPolicy.optimizer_trial`（复用 `mining_runtime` 的矩阵共享与裁剪模式），objective 默认 `sharpe`（与挖掘 gate 同口径），必须属于 `guards.PRIMARY_OBJECTIVES`。
3. 当前分 = `robust_optimizer.robust_score`（0.6×median + 0.4×worst）。
4. 退化判定（方向感知，公式与 `robust_optimizer` stage2 的 degradation 同语义，min 类目标不误判）：
   - `healthy`：退化 ≤20% 且软底线未击穿（sharpe>0、回撤不差于 -30%、n_trades ≥ 基线 1/3）。
   - `watch`：退化 20%–50%，或击穿一项软底线；连续 2 次 watch 触发通知。
   - `retired`：退化 >50%，或硬底线击穿（n_trades 枯竭到接近零——因子失效最常见的形态是"不再触发"）。
5. 退役动作**只改档案状态 + 系统通知 + 前端置灰**，不自动删除/移动策略文件（用户数据安全，`CONTRIBUTING.md` §8）；提供人工"归档"按钮，走策略删除的既有 fail-closed 校验路径。retired 不自动回场。

### 3.4 执行模型

- 新 worker kind `"guardian"`：`worker.py` `make_worker_task` (L129) 加一个分支，子进程内逐策略复验（全市场矩阵一次 `prepare_matrix_optimization`，各策略按需重建——策略 universe/评分不同，矩阵不能跨策略复用，但同策略的 K 段用 `market_data_override` 只读视图复用，模式同稳健优化器）。
- 并发槽：与挖掘共用 `shared_heavy_job_limiter.slot("mining")`——两者都是全市场级内存任务，串行化避免双份 1.3GiB 矩阵。
- 调度：`guardian_schedule.run_daily_guardian()`，镜像 `mining_schedule` 的 claim 模式（claim id `guardian-daily-{date}`，enriched 数据指纹无变化则跳过）。挂在主进程现有后台调度线程上。
- `task_registry.TASK_KINDS` 增加 `"guardian"`（前端 `taskRegistry.ts` 同步）。

### 3.5 API 与前端

- `backend/app/api/guardian.py`：`GET /api/backtest/guardian/overview`（全部守护策略：状态、基线 vs 最近、退化、曲线端点）、`POST /{sid}/adopt|drop|rerun`、`GET|PATCH /config`（阈值、objective、K、开关走 preferences）。
- 前端：回测页"稳健性"域新增子 tab **守护** → `frontend/src/pages/backtest/StrategyGuardian.tsx`（`RobustnessValidation.tsx` 加一个子 tab，路由不动）。表格 + 状态徽章 + 基线/近期对比 + 各段分数；rerun 走轮询（同 `miningTask` 模式），不需要 SSE 镜像。

---

## 四、P2 组合进化

### 4.1 稳健选冠（2a）

- `mining_runtime` 内给 `MatcherCandidateEvaluator` 的外层聚合换语义：冠军排序分从"外层折 Sharpe 均值"改为 `robust_score(各外层折 OOS sharpe)`（median+worst），并把逐折分散度（worst/best）写入候选 meta。
- 晋升门槛增加可选稳健维度：strict 档要求 worst-fold sharpe > 0（默认开）；gate 的绝对阈值不变。
- **版本纪律**：选冠语义变化会改变同请求的结果，落地时 `MINING_ALGORITHM_VERSION` 升为 `mining-v3`，并进数据指纹，防止升级后 claim 幂等复用旧口径结果。

### 4.2 冠军权重精调（2b）

- 对束搜索最终冠军组合（≤4 因子），权重在 `{0.5, 1.0, 1.5, 2.0}^n`（≤256 组合）网格上做 K 段锚定验证（完全复用 `RobustOptimizeService` 的两段式：stage1 全网格分段，stage2 Top-N 全窗口复验退化），选出稳健权重写入发布策略的 `scoring`。
- 矩阵复用挖掘 run 已准备的全区间超集矩阵（同策略/universe/区间，签名校验天然通过），精调增量成本 ≈ 一次稳健优化的 stage1。
- 预算：精调 trial 计入 `MiningBudget`，不另开上限。

### 4.3 维度扩展（2c/2d，按需启动，默认不做）

- 2c overlay 候选：对最终冠军试 ≤4 个预定义 overlay 模板（排除高位、排除低流动性等）。注意 overlay 改变 universe，每个变体需重建矩阵，成本 ×变体数，只在 strict 档开放。
- 2d 策略级组合：守护档案中 ≥2 个 healthy 冠军且**日收益相关性 ≤0.7**（新增策略收益相关性计算，因子相关性剪枝 `compute_rank_correlation` 不直接适用）时，前端提示"可组合"，人工确认后生成 composite 策略（引擎已支持 children ≤8）并纳入守护。不自动生成，防组合爆炸与过拟合叠加。

---

## 五、P3 闭环编排

- `lab_schedule.py` 统一状态机：`idle → screen → mine → gate → publish → watch → retire(腾名额) → idle`。两个 claim：`lab-weekly-{iso}`（挖掘+精调）、`lab-daily-{date}`（守护复验），替代原 `mining_schedule` 入口（保留旧入口兼容一个版本）。
- 名额上限：活跃 mined 策略数 preference（默认 10）；名额满时不发布新冠军，产出 pending 候选等位。
- 战报：守护 + 挖掘 summary 聚合为前端总览卡（本周新挖掘 run、晋升/退役/在役数、平均退化、最弱在役策略）。

---

## 六、关键设计决策与风险

| 决策 | 理由 |
| --- | --- |
| 守护层数据绝不反馈进参数选择 | 逐层 OOS 纪律；否则"用未来调参"，守护退化判定失去意义 |
| 退役不自动删策略文件 | 用户数据安全；删除/归档是高风险操作，必须人工确认 |
| 手动入护基线标 `trailing_reference` | 尾随窗口不是 OOS，语义必须显式区分，否则误判退化 |
| guardian 与 mining 共用 heavy 槽 | 两个全市场矩阵并发会破 RSS 门槛；串行化是唯一安全选项 |
| 2a 落地必升算法版本号 | 同请求不同口径的结果混用 claim 幂等缓存是数据事故 |
| objective 只取 `guards.PRIMARY_OBJECTIVES` | win_rate 等退化目标做主目标是已知反模式 |
| 复验窗口 <30 交易日跳过 | 段太短的 robust 分是噪声，判定只会制造假告警 |
| generation 快照 + 重试读面板 | 沿用挖掘运行时既有纪律，撞上 enriched 发布 fail-closed |

剩余风险：守护判定阈值（20%/50%）是经验值，需在 P1 上线后按实际分布校准；策略收益相关性（2d）已定为日收益 Pearson（≥30 共同交易日）。

---

## 七、测试矩阵

| 改动 | 最低验证 |
| --- | --- |
| 判定状态机（纯函数） | healthy/watch/retired 边界、方向感知（min 目标）、n_trades 枯竭、退化百分比边界 |
| 基线写入 | publish 后档案存在且指标与 gate 一致；发布失败不写档案；手动入护标 trailing_reference |
| 调度 | claim 同日防重、数据指纹不变跳过、新增数据 <30 日跳过 |
| worker/API | kind 注册、overview/adopt/drop/rerun 契约与错误路径、generation fail-closed |
| 2a 选冠 | median+worst 与均值排序产生不同冠军的构造用例；版本号 bump 断言 |
| 2b 精调 | 网格 ≤256、与 robust_score 复用一致性、预算计入 |
| 前端 | `pnpm build`；守护页加载/空/错/watch/retired 状态 |
| 回归 | `test_mining_*.py` 全量 + 新增 `test_strategy_guardian*.py` |

## 八、分期与验收

- **P1（先行）**：发布基线自动落档 + 每日复验 + 三态判定 + 守护页。验收：对一个已发布 mined 策略跑通"发布→复验→人工注入退化数据→watch/retired 告警"全链路（测试内构造），真实数据下守护页正常展示。✅ 已实施并通过真实数据 E2E。
- **P2a+2b**：稳健选冠 + 权重精调 + 版本号升级。验收：同一历史 run 请求在新旧口径下冠军可不同且均有记录；精调后发布策略的 scoring 与档案一致。✅ 已实施（`mining-v3`，精调差异见文首落地口径）。
- **P2d、P3**：✅ 已实施（组合建议扫描/人工确认生成 composite + 自动入护；lab 调度单入口 + 名额上限 + 战报聚合 `/api/backtest/lab/report` + 守护页战报卡）。
- **P2c**：按需启动，默认不做（见文首说明）。
