# ARCHITECTURE.md

本仓库采用“生产后端、研究代码、数据、研究产物”四层分离。所有后续后端开发与量化实验必须遵守本文件。

## 1. 仓库结构

```text
tickflow-stock-panel/
├─ backend/
│  ├─ app/                    # FastAPI 生产应用
│  │  ├─ api/                # HTTP API，仅做协议适配与参数校验
│  │  ├─ backtest/           # 回测模型、矩阵、引擎和 worker
│  │  ├─ data_providers/     # 数据源适配与标准化
│  │  ├─ jobs/               # 调度任务
│  │  ├─ services/           # 应用服务与业务编排
│  │  ├─ strategy/           # 策略注册、生成、执行和 builtin
│  │  └─ tickflow/           # TickFlow SDK 适配
│  ├─ research/              # 可复现研究代码，不进入生产依赖
│  │  ├─ common/             # 研究共享的纯计算模块
│  │  ├─ factors/            # 因子搜索与 OOS
│  │  ├─ alphagpt/           # CPU-only 公式环境、候选池、奖励与搜索
│  │  ├─ regime/             # regime 条件化研究
│  │  ├─ optimization/       # 历史参数优化
│  │  ├─ selection/          # 可解释选股评分、点时消息契约与逐股审计
│  │  ├─ validation/         # 区间复验与 walk-forward
│  │  ├─ reporting/          # 报告生成
│  │  └─ legacy/             # 仅为复现保留的退役研究入口
│  ├─ scripts/               # 运维、修复和数据维护命令
│  ├─ tests/                 # 自动化测试
│  ├─ pyproject.toml
│  └─ uv.lock
├─ artifacts/
│  ├─ current/               # regime 三项 + MVP 两项白名单产物
│  ├─ archive/               # 按研究主题归档的历史产物
│  └─ logs/                  # 本地日志，不提交
├─ data/                     # 用户数据与缓存，不提交
├─ docs/                     # 使用、部署、插件等文档
│  ├─ assets/                # 文档资源
│  ├─ archive/               # 已下线功能的历史资料
│  └─ examples/              # 文档示例
├─ scripts/                  # 仓库级数据接入工具
├─ packaging/                # 桌面/安装包构建
├─ AGENTS.md                 # AI/自动化代理入口
├─ ARCHITECTURE.md           # 目录、依赖和命名规范
├─ HANDOFF.md                # 当前人工交接入口
└─ README.md                 # 项目入口
```

## 2. 依赖方向

允许的依赖方向：

```text
api/jobs -> services -> backtest/strategy/data_providers/tickflow
research -> app
tests -> app/research
```

禁止：

- `app/` import `research/`。
- 生产服务读取 `artifacts/` 作为运行时状态。
- API 路由直接实现复杂回测、数据加工或策略逻辑。
- 研究脚本在仓库根目录写 JSON、HTML、Markdown 或日志。
- 将运行时数据提交到 Git。

## 3. 生产后端边界

- `api/`：解析请求、鉴权、调用 service、序列化响应；不放可复用业务逻辑。
- `services/`：跨模块业务编排、缓存与外部能力调用。
- `backtest/`：回测契约、执行与指标计算；不得依赖具体 API。
- `strategy/`：策略定义、参数规范化与信号生成。
- `data_providers/`：不同数据源到项目标准 schema 的适配。
- `tickflow/`：仅封装 TickFlow SDK、能力与限流策略。
- `scripts/`：一次性运维命令；若逻辑会被应用复用，先下沉到 `app/`。

新增模块前，优先扩展现有边界。只有出现独立职责和多个调用方时才新建顶层包。

### 3.1 矩阵策略组合 v1

- `app/strategy/composition.py` 定义组合协议和纯矩阵合成，不负责加载行情或注册策略。
- `StrategyBacktestConfig.composition` 是可选入口；为空时保持原单策略行为。
- 组合仅接受 2–8 个 `matrix_native` 策略，禁止重复；首个 component 必须与
  `strategy_id` 相同，并作为止损、持仓上限、敞口等组合级风控的唯一所有者。
- 开仓支持 `and` / `or`；退出固定为任一 component 退出即退出。不同策略的原始
  score 不可直接相加，因此先按交易日做截面百分位，再按正权重合成。
- 组合所需字段与 warmup 由各 component 的 `StrategyDependencyResolver` 结果取并集，
  行情矩阵仍只准备一次。component 继续走原 `MatrixStrategyPipeline`，不复制过滤或评分实现。
- v1 不支持非矩阵策略、负权重、任意嵌套和组合级动态脚本。组合能力通过后端回测 API
  暴露，不新增前端代码。
- 组合引擎通过单测只说明执行语义正确，**不代表组合收益已被 OOS 验证**；任何权重、
  entry mode 或公式选择仍须在训练折确定，再用封存测试折验收。

### 3.2 策略目录生命周期

- `app/strategy/catalog.py` 是生产策略发现层的唯一生命周期事实源；交易逻辑仍留在
  `app/strategy/builtin/`，目录分类不得改变显式 ID 的执行能力。
- 生命周期分为 `core`、`tool`、`experimental`、`legacy` 和用户策略的 `user`；同时
  暴露 `visible_by_default` 与 `evidence_status`，不能用“内置”冒充“已验证”。
- 默认发现和未指定 ID 的批量执行只包含 3 个当前 core（均线多头、趋势突破、回踩
  支撑）及用户策略；超卖反转、涨停动量因连续历史复验失败已降为 experimental。
  `include_experimental=true` 显式展开全部。详情、回测、监控和显式批量 ID 不受隐藏影响。
- 新 builtin 未归类时必须安全降级为 `legacy/unverified/hidden`，不能自动进入默认清单。
- 当前 core 是产品去重后的代表性入口，不是收益晋级名单；其中仍包含未验证或历史复验失败项。

### 3.3 Tushare 增量数据同步

- 权威入口是 `backend/scripts/tushare_sync.py`，从 `backend/` 以
  `python -m scripts.tushare_sync` 运行。默认只补缺失交易日；已有有效股票日线
  必须跳过，不允许为了补数先删除整个 `kline_daily/`。
- 新分区先写同目录临时文件，验证 schema、日期、`symbol/date` 唯一键和 OHLCV
  合法性后再原子替换。失败请求不得留下半个正式分区。
- 股票日线写 `data/kline_daily/`，指数日线合并到 `data/kline_index_daily/`，
  Tushare 每日指标单独写 `data/tushare_daily_basic/`；token 不进入日志、manifest
  或仓库。
- `--workers N` 只并发网络请求，规范化、校验和原子落盘仍按交易日顺序执行。
  交易日历开放但行情为空时不得伪造分区，必须写入 manifest 的对应失败列表。
- 复权因子增量必须同时拉缺口前一交易日的累计因子，再计算缺口首日的
  `ex_factor`；禁止把增量首日无条件写成 1。
- 根目录旧 `scripts/ingest.py --tushare` 默认委托安全增量入口。只有显式
  `--clear-first` 才允许旧的全量清空重建路径。

### 3.4 结构行情策略切换

- `research/regime/market_structure.py` 用全市场个股日收益、站上 MA20/MA60 的比例
  和迟滞生成结构牛/结构熊标签；交易日 t 只读取 t-1 或更早收盘数据。
- 特征必须读取研究起点之前的行情完成 MA/收益暖机，再过滤输出研究期标签；不得
  先裁到 2024-09-24 后再计算滚动窗口。当前研究期无 warmup。
- 等权市场路径必须先算逐股票收益再做截面均值并复合，不能平均不同股票的价格水平。
- 可重建标签缓存写入 `data/.regime_cache/market_structure_v1.parquet`；审计事实源
  写入 `artifacts/archive/regime/market_structure_v1.json`。生产代码不得读取 artifact。
- `composition.entry_mode="regime_switch"` 只接受两个 matrix-native component：
  第一个是结构牛腿，第二个是结构熊腿；翻转日退出旧腿。`regime_filter.type=
  "market_structure_v1"` 可将结构熊腿设为现金。
- 能力可用不等于收益有效。P13 历史重放中所有切换候选都只在 1/4 折战胜对应
  全时段基线，因此默认不启用、不得写回生产策略参数。

### 3.5 可解释选股 v1

- `app/strategy/builtin/quality_momentum_v1.py` 是唯一可执行评分事实源；研究审计直接
  调用其 `compute_quality_components`，不得复制另一套公式。
- 评分由趋势、动量质量、量价确认、流动性与五项风险扣分构成。逐股硬门槛失败原因
  必须显式输出，不能只保留前 N 名。
- `research/selection/` 负责历史同预算对照、当前候选解释和 point-in-time 消息契约。
  生产 `app/` 不得反向依赖该目录。
- 早盘只能通过 `research.selection.run_auction_selection_v1` 在上一完整交易日候选之上
 叠加 9:25 集合竞价确认。原始快照只写入 `data/tushare_auction/`；盘中数据不得写入
  `kline_daily/` 或 `kline_daily_enriched/`，也不得用于改拟合基础评分或历史阈值。
- 依赖竞价成交额的策略不得伪装成标准日线策略。`first_board_second_day_v1` 的纯规则放在
  `app/strategy/specialized/`，专用 I/O 与审计入口放在 `research/selection/`；它不进入
  core 默认策略清单，历史竞价数据不完整时只能标记为 `LIVE_SCREEN_ONLY`。
- 当前行业文件没有历史生效区间，只可用于最新截面的展示与分散约束，禁止进入历史
  评分。消息没有带发布时间的历史库时必须关闭。
- `quality_momentum_v1` 归为 experimental/hidden。历史回放有改善也不能替换 core；
  必须从冻结注册后的新交易日起积累 fresh OOS。

## 4. 研究代码规则

- 新实验必须放进 `backend/research/<domain>/`，命名为 `run_<topic>.py`；诊断脚本使用 `diag_<topic>.py`。
- `<topic>` 必须写明研究对象，版本化协议使用 `_v1/_v2`；禁止新增 `run_opt_v2.py`、
  `run_iterate.py`、`run_walkforward.py`、`run_range_bt.py` 等脱离目录就无法理解的名字。
- 已退出路线但仍需复现的脚本放 `research/legacy/<domain>/`，使用描述性文件名。legacy
  只允许修复复现错误，不得承载新实验、写入 current 或晋级生产。
- 旧模块路径仅可保留为调用 `research.legacy` 对应 `main()` 的薄兼容入口；映射见
  `research/legacy/README.md`，兼容文件中禁止加入研究逻辑。
- 共享研究逻辑放 `research/common/`，稳定路径只从 `research.paths` 读取。
- 从 `backend/` 使用模块方式运行：

```powershell
.\.venv\Scripts\python.exe -m research.<domain>.<module>
```

- 因子与策略选择必须区分训练和测试区间；optimization 结果不得自动升级为 OOS 结论。
- 历史五策略参数搜索统一从 `research.optimization.run_core_strategy_walkforward_v1`
  进入：每折只用 180 日训练段按 Calmar 选参，再在相邻 60 日测试段冻结评估；默认
  “无信号”必须按持币 0%/0 笔交易计入，不能删除该折。该历史 replay 只更新证据
  标签和下一观察期候选，不得写回生产默认参数。
- 仓位数量与分配方式通过 `research.optimization.run_core_portfolio_walkforward_v1`
  单独研究，不与入场参数混扫。固定候选为等权 3/5/10/20 只和评分加权 5/10/20 只；
  每折训练目标为 `return - 0.5 * abs(max_drawdown)`，至少 30 笔训练交易。测试折不参与
  候选选择；历史 replay 即使改善也不能自动写回生产配置。
- 核心策略冻结观察统一由 `research.validation.run_core_strategy_forward_watch_v1`
  执行。校准截止日固定为 2026-06-30，观察从 2026-07-01 开始，只允许随数据增长
  延长结束日；协议哈希不匹配时拒绝覆盖。少于 60 个新交易日只能是 `PENDING_DATA`，
  达到 60 日也只允许人工冻结复核，永不自动晋级。
- 退出/风控研究从 `research.optimization.run_core_exit_walkforward_v1` 进入，固定比较
  默认、3%/10%止损、8/30日持仓、移动止盈及止损+持仓组合。共享行情矩阵只用于
  加速，折内配置仍独立解析执行；历史折通过门槛也只能从最后已见数据次日开始观察。
  当前五个 core 全部未通过，因此没有新增观察候选或生产覆盖。
- `regime_filter.type="market_breadth"` 使用当前回测 universe 的前一日站上 MA20/MA60
  比例和迟滞阈值生成状态，再复用既有 soft exposure/scale-existing 通道。默认关闭；
  P12 三候选历史复验为 -23.34% 对默认 -0.90%，已拒绝且不得继续补扫阈值。
- P13 结构行情从 `research.regime.run_market_structure_v1` 生成因果标签，再由
  `research.regime.run_structure_strategy_replay_v1` 同次比较牛腿
  `trend_breakout/bullish_alignment` 与熊腿 `cash/pullback_to_support`。当前全部
  切换方案未通过；标签可用于描述，不能据此宣称策略切换有效。
- P14 由 `research.validation.run_structure_strategy_forward_watch_v1` 冻结
  `trend_always/pullback_always`。注册日之前已可见的数据不得计入 fresh 门槛；
  2026-07-30 起按同一400股 universe 记录收益、基准超额、结构归因和换手代理。
  60/120日门槛只控制人工复核准备度，禁止自动晋级或反向调参。
- `research.optimization.run_structural_bull_challenge_v1` 是已观察目标窗口上的挑战实验，
  不是 OOS 优化器。它允许报告样本内 60%/80% 是否可达，但候选冻结后必须原样跑既有
  强牛窗口；无论目标窗口多漂亮都不得自动改生产参数。当前结论为过拟合拒绝。
- seeded universe 抽样统一调用 `research.common.universe.stable_symbol_sample`；禁止对
  Polars `unique()` 的未排序输出直接 `sample()`。研究产物必须记录完整 symbol manifest、
  universe size、seed、日期范围、选择方法和 SHA-256；写 sidecar 时使用原子替换。
- 历史 replay 与 fresh OOS 必须在产物中分开。组合验证少于 60 个新交易日时状态固定为
  `PENDING_DATA`；60–120 日只允许进入冻结复核，不能自动晋级。
- `research/alphagpt/` 的生成、筛选、调参、断点和 early stopping 只能读取训练折；
  HOLDOUT 必须在排名冻结后由独立报告函数读取。
- AlphaGPT 策略层可以从 random/evolution 升级为 Transformer/PPO，但必须复用
  `AlphaEnv` 动作掩码、`StackVM`、`FactorPool`、`RobustReward` 和既有 OOS 评估，
  不得复制第二套 DSL 或让 `app/` 反向依赖 `research/`。
- 学习策略必须通过 `research.alphagpt.policy.TokenPolicy` 接入；模型 logits 统一由
  `MaskedLogitPolicy` 应用 `AlphaEnv.action_mask`。`policy.py`、`rollouts.py` 和
  `dataset.py` 保持无 torch 依赖，模型框架放独立训练模块。
- rollout 数据只能从训练候选池和训练奖励生成；不得复制最终候选中的 HOLDOUT
  指标。JSONL 和 manifest 归档到 `artifacts/archive/factors/`。
- 无外部 ML 依赖的基线模型放 `research/alphagpt/behavior_clone.py`；训练入口只
  读取 rollout train/validation split。模型 checkpoint 与审计报告写入
  `artifacts/archive/factors/`，不得写入源码目录或生产 `app/`。
- 多 seed rollout 必须保持同一训练 universe、折区间和 evaluator，并在跨 seed
  合并时按规范化公式哈希去重。稳定性 gate 只能使用 rollout validation 与
  T1–T3 奖励；gate 未通过时不得接 PPO。
- reward weighting、elite filtering 和后续 reward model 只能读取训练 episode
  的 `final_training_reward`。不得在同一 validation split 上无限扫描阈值来挑选
  有利结果；每个阶段必须预先落盘固定配置与失败 gate。
- 公式级 reward model 只能使用规范化公式结构特征；alpha 等超参数必须在 train
  split 内交叉验证。validation 只允许用于锁定后的一次 rank/top-k gate，公式哈希
  必须跨 split 唯一并与 token 一致。
- reward-model reranker 必须在未见公式与新 seed 上前瞻验证，并与相同候选池、
  相同真实 evaluator 调用预算的随机选择对照。当前 P11-D reranker 绝对奖励 gate
  为 FAIL，不得接入 token policy、evolution elite selection 或 PPO。
- 随机公式 reward-label 数据必须按完整生成 seed 划分 train/validation，不能把
  同一 seed 的公式散切到两侧。公式固有标签固定为相关性惩罚为零的 T1–T3
  RobustReward；依赖候选池的相关性惩罚只作为 operational audit，不作为
  formula-only 模型目标。
- pairwise/listwise 目标和 ridge 强度只能通过 train seed 留一 CV 选择。P11-E
  validation gate 已失败，不得补扫该 validation、运行前瞻 reranker或接入 PPO。
- `run_release_v1.py` 是 AlphaGPT Research v1.0 的发布边界：只读取并校验归档
  产物，不重新训练或改写历史实验。发布 manifest 必须包含 SHA-256、能力/非能力、
  gate 状态和统一复现命令；完整研究版本不得被表述为生产 alpha。
- 当前权威产物只能写入 `artifacts/current/` 的显式白名单；中间和历史产物写入对应的 `artifacts/archive/<domain>/`。
- 大日志写入 `artifacts/logs/`，不要与源码同目录。

## 5. 数据与产物

- 唯一研究数据根目录是 `data/`。
- `data/kline_daily_enriched/` 是当前因子和 regime 研究的行情入口。
- `.regime_cache/` 等缓存属于可重建数据，不是事实源。
- `artifacts/current/` 只保留：
  - `regime_ensemble_report.html`
  - `strategy_regime_ensemble.json`
  - `diag_f4_regime.json`
  - `mvp_backtest.html`
  - `mvp_backtest.json`
- 历史文件必须归档，不得与 current 混放。

## 6. 测试与验证

- 生产代码改动：运行对应的 `backend/tests/`，必要时再跑完整 pytest。
- 研究公共模块改动：至少做 import/compile 检查和一个最小数据窗口 smoke test。
- 策略或回测改动：保留同一次运行中的基线与候选对照。
- 报告生成器改动：核对输入 JSON、生成 HTML，并确认关键数值一致。
- 目录或路径改动：执行所有研究模块的 import 检查，确保没有 `Path(__file__).parent` 推导项目根。

## 7. 新增文件放置决策

```text
是否参与 FastAPI 生产运行？
├─ 是：backend/app/<existing-domain>/
└─ 否
   ├─ 可复现实验：backend/research/<domain>/
   ├─ 运维/修复命令：backend/scripts/
   ├─ 仓库级数据导入：scripts/
   ├─ 文档：docs/
   ├─ 研究结果：artifacts/current 或 artifacts/archive/
   └─ 运行时数据：data/
```

无法明确归类时，先补充职责说明，不要把文件临时堆在仓库根目录或 `backend/` 根目录。

## 8. Selection MVP v2 边界

当前选股链保持“生产实现、研究评估、运行时数据、归档证据”四层单向依赖：

```text
data/kline_daily_enriched + tushare_stock_basic
  -> research.selection.run_selection_mvp_v2
  -> app.strategy.builtin.quality_momentum_v1 / custom_factor (只调用生产实现)
  -> research.selection.mvp_v2 (标签、Top-K、walk-forward、成本与统计)
  -> artifacts/archive/selection (JSON + CSV 审计)
```

`app/` 不导入 `research/`。因子只允许作为 selector 的可选 overlay；一进二仍是独立的
9:25 竞价 specialized runner，不与日线横截面标签混合。结构牛熊只在 P15 结果生成后
做诊断归因，不进入评分、折内选择或参数搜索。

P15 的执行时点固定为收盘 `t` 计算、开盘 `t+1` 买入。任何 forward label 都在评分和
排名之后构建；涨停无法买入、跌停无法退出、停牌和缺失行情显式标记为缺失成交，不能
按未来可成交性补位。P15 最初缺少历史证券状态，只能使用当前名称代理；该缺口由下面
的 P16 ST 日表修正，旧代理结果仅作为偏差对照保留。

P16 已把 ST 维度升级为逐交易日事实表：

```text
Tushare stock_st
  -> scripts.tushare_sync (校验、断点续跑、原子分区)
  -> data/tushare_stock_st/date=YYYY-MM-DD/part.parquet
  -> research.selection.point_in_time_universe_mask
  -> P15 冻结逻辑的同预算 proxy/PIT 对照
  -> P16 历史证据 + forward-watch 冻结协议
```

PIT runner 对研究范围内每个交易日做严格覆盖检查，禁止缺口回退。历史行业分类和完整
证券主数据变更史仍未提供，因此“ST 维度 point-in-time”不等于整个证券主数据已经完全
无偏。前向观察只追加校准截止日之后的数据；观察脚本不得修改生产目录或自动晋级。
