# 回测成熟度审计与改进计划

> 状态：实施中（V1 可信度修复、V2 实验闭环已落地；V3 专业报告能力已显著完善，剩余边界见 §12）  
> 创建日期：2026-08-19 ｜ 最近复审：2026-08-19  
> 适用范围：`/backtest` 策略、因子、组合策略、参数网格及其后端回测链路  
> 目标：把当前“单次回测结果查看器”升级为口径统一、可复现、可比较、可审计的策略研究工作台。

## 1. 结论摘要

本项目已经具备较完整的回测主链路：内置/自定义/AI/组合策略统一接入；建仓与清仓成交口径可分别配置；支持费用、滑点、资金、仓位和六种权重模型；处理 T+1、整手、涨停无法买入、跌停/停牌无法卖出及 pending exit；结果页提供净值、基准、回撤、收益分布、交易明细、选股分析和单笔 K 线回放；参数网格已有 Bootstrap Sharpe 区间、置换检验和退出原因分析。

当前定位更接近“功能较强的个人量化研究工作台”，尚未达到成熟产品的可审计实验平台水平。剩余核心边界不是策略或指标数量，而是：

1. 全市场股票池仍无法证明历史时点成分，存在幸存者偏差；
2. 撮合尚未模拟成交量冲击和部分成交；
3. Fama-French 归因缺少冻结且可审计的本地因子收益序列，必须继续 fail-closed；
4. Walk-Forward 只在受限局部候选空间内选参，不是全局优化。

后续建设顺序固定为：

> **统一口径 → 冻结数据 → 保存 Run → 对比 Run → 样本外验证 → 导出报告**

在可信度修复和实验闭环完成前，不继续扩充新的单日技术指标策略。

**进展（2026-08-19）**：V1 可信度修复七项与 V2 实验闭环六项已全部落地并有对应测试文件；V3 已落地统一专业诊断（高级风险/相对基准指标、滚动稳定性、月度热图）、参数热力图与收益–回撤 Pareto 前沿（以收益/夏普/回撤绝对值三目标做严格非支配分层，独立于目标得分排序）、持仓期 MAE/MFE、独立 HTML 研究报告与结果页打印，以及严格 IS/OOS Walk-Forward（训练选参→冻结参数→独立 OOS，含拼接 OOS 净值与参数漂移；候选为 baseline + 单参数 ± 局部邻域，非全局优化）。交易明细已支持代码/名称、盈亏、退出原因和分页筛选；交易窗口 Brinson-Fachler 行业归因已接入结果页。因子回测通过共享服务端任务的 SSE 提供进度回放、显式取消及刷新/路由重连，完成结果仍按唯一 `BacktestRun` 契约持久化，持久化失败显式可见。行业映射为调用时刻分类、非 point-in-time；Fama-French 在没有冻结且可审计的本地因子收益序列时显式 unavailable，绝不生成代理结果。参数网格仍为受局部边界限制的候选空间，Pareto 前沿只说明样本内非支配关系、不构成全局最优证明。逐项状态与证据见 §11、§12。

## 2. 当前成熟度

评分标准：0=缺失，1=基础，2=可用，3=成熟。下表为 2026-08-19 本轮落地后的复审值（初评见 git 历史）。

| 维度 | 评分 | 结论 |
|---|---:|---|
| 策略配置 | 2.5 | 参数、过滤、触发器、评分、风控、环境过滤和组合策略较完整 |
| A 股撮合与资金约束 | 2.0 | T+1、涨跌停、停牌、整手、费用和滑点可用；缺成交量冲击和部分成交 |
| 任务执行 | 3.0 | 策略与因子均通过共享服务端任务提供 SSE 进度、显式取消、切页保持与刷新重连；相同参数只计算一次、完成结果按唯一 Run 契约持久化 |
| 单次结果展示 | 2.5 | 净值、回撤、基准、交易、收益分布、K 线回放加专业诊断（滚动/月度/相对基准/MAE/MFE）与独立 HTML 报告；Brinson-Fachler 已可用，Fama-French 显式不可用 |
| 多实验对比 | 3.0 | BacktestRun 历史/收藏/标签/搜索、2~4 Run 指标矩阵与曲线对比、导出、复跑、参数网格回填 |
| 专业指标 | 2.5 | 统一 MetricContext 消费全套高级指标；Profit Factor/Payoff Ratio 分离；持仓期 MAE/MFE 可追溯到逐笔交易；Fama-French 仍缺可审计因子收益序列 |
| 可复现与审计 | 2.5 | 数据快照、engine/metric 版本、随机 seed、不可变 Run 持久化；历史时点股票池仍不可证（显式告警） |
| 防过拟合 | 2.5 | seed 可复现、参数扰动、Bootstrap、置换检验、参数热力图与前沿；严格 Walk-Forward（训练选参→冻结→OOS）已落地，候选为局部单参数邻域 |

## 3. 现有强项

### 3.1 策略配置

`frontend/src/pages/backtest/StrategyBacktest.tsx` 已覆盖：

- builtin/custom/ai/composite 策略；
- 类型化参数及 min/max；
- 价格、成交额、市值、换手、ST、板块过滤；
- 买卖触发器、评分权重及评分区间；
- 止损、止盈、移动止损、回撤止盈、最长持仓；
- T-1 市场环境过滤；
- 初始资金、最大持仓、总仓位；
- 等权、评分加权、等波动、风险平价、均值方差、最大分散化；
- 仓位模拟与独立候选模拟；
- 条件选股到回测的股票池交接及起始日钳制。

### 3.2 撮合与执行

`backend/app/backtest/engine.py` 已处理：

- `close_t` / `open_t+1` 独立建仓、清仓口径；
- 双边费用和固定 bps 滑点；
- 100 股整手；
- T+1、同日卖出后禁止回补；
- 涨停禁买、跌停禁卖、停牌阻塞；
- pending exit；
- 风控退出、信号退出、最长持仓的优先级；
- 最大持仓、最大总仓位和资金约束；
- 成交阻塞原因统计。

### 3.3 结果与钻取

策略**仓位模拟**结果页已提供：

- 总收益、年化、同期基准、简单超额、Sharpe、最大回撤、胜率、交易数；
- 成交约束摘要；
- 净值/基准/回撤图；
- 收益分布；
- 每日交易、逐笔交易和按标的聚合；
- 退出原因、阻塞天数、买卖信号日期；
- 单笔交易 K 线回放。

全量独立执行则只展示候选交易级统计及按退出事件日聚合的样本收益曲线；它不生成账户年化、Sharpe、基准/超额或相对绩效指标，不能与仓位模拟净值直接比较。

因子回测已有 IC、ICIR、IC 胜率、IC 时序、分层净值和多空统计。参数网格已有 24 默认/36 硬上限场景、四种目标函数、场景排名、Bootstrap、置换检验和退出原因分布。

## 4. 必须优先修正的专业口径

### P0-1 因子成本配置未生效（✅ 已修复）

因子配置接受费用与滑点，但分层/多空净值未扣费。必须按调仓换手扣除成本；在实现完成前不得把毛收益展示成净收益。

> 落地：`app/backtest/factor.py` 按单边换手 `turnover = traded_notional / 2` 与单边成本率 `one_way_cost = fees_pct + slippage_bps / 10000` 扣费，净收益 `net = (1 + gross) × (1 − cost) − 1`；多空两腿均按正成本扣减，绝不用负费用加收益。测试 `tests/backtest/test_factor_costs.py`。

### P0-2 年化与 Sharpe 口径不统一（✅ 已修复）

组合、稳健性和因子路径存在 `ddof`、风险自由利率及周/月频年化系数差异。建立统一 `MetricContext`：

- `return_frequency` 是唯一频率输入（`daily`/`weekly`/`monthly`）；
- `periods_per_year` 由频率派生（252/52/12），不允许调用方双写；
- `risk_free_rate` 显式记录，默认 0；
- `std_ddof` 统一为 1。

所有具备等间隔收益序列的回测、因子、网格和稳健性分析统一调用同一指标入口。若兼容入口同时给出频率与年化系数且两者不一致，必须拒绝而不是静默选择。

> 落地：`app/backtest/metrics.py::MetricContext`（含 `version`，随具备等间隔收益序列的结果与 Run 输出）；引擎、因子、稳健性、策略与 API provenance 全部经同一入口年化。候选独立执行的例外见 P0-2a。测试 `tests/backtest/test_metrics.py`。

### P0-2a 全量候选模式的伪日频指标（✅ 已修复）

全量独立执行的曲线按交易**退出事件日**聚合，不是连续交易日账户净值；若把它当作日频收益，会伪造年化、Sharpe、波动、Alpha/Beta/IR、基准和超额。

> 落地：`engine.py::_calc_independent_candidate_result` 保留交易级 Profit Factor、Payoff、胜率、MAE/MFE 等统计，但将年化/Sharpe 明确置空且不产生时间序列高级指标；`strategy.py` 跳过基准与相对指标；`api/backtest.py::strategy_robustness` 对 `mode=full` 在构造回测服务前返回 422，前端明确提示该分析不适用；`parameter_grid.py::run_grid` 对候选执行的 best 场景跳过 Bootstrap/置换 Sharpe，仅保留交易级退出原因并标记 `time_series_metrics_unavailable`，前端明确提示日频指标不适用；`api/backtest.py` 不写入 `metric_context` 并追加 `candidate_return_curve` 告警；结果页和 HTML 报告把曲线标为候选样本而非账户净值。测试 `tests/backtest/test_strategy_backtest_correctness.py`、`tests/backtest/test_parameter_grid.py`、`tests/api/test_strategy_robustness_api.py`、`test_provenance.py`。

### P0-3 Profit Factor 命名错误（✅ 已修复）

当前部分路径使用“平均盈利 / 平均亏损”，它是 payoff ratio，而非标准 Profit Factor。新契约必须同时输出：

- `payoff_ratio = avg_win / abs(avg_loss)`；
- `profit_factor = gross_profit / abs(gross_loss)`。

> 落地：`metrics.py` 分离 `payoff_ratio` 与 `profit_factor` 两个独立函数，策略/全量模拟 stats 同时输出两者。

### P0-4 随机稳健性不可完全复现（✅ 已修复）

普通策略稳健性端点没有固定 seed。seed 必须进入请求、结果和 Run 元数据；默认从配置哈希与数据版本派生。

> 落地：`/strategy/robustness` 请求支持显式 `seed`（0 ~ 2^63−1）；缺省由配置哈希 + 数据版本派生，seed 写入响应 `random_seed` 与 Run 元数据。

### P0-5 数据与股票池未冻结（✅ 快照元数据已落地；point-in-time 股票池仍不可证）

V1 先把下列字段写入现有 run card 与策略结果响应；V2 再迁移到唯一的 `BacktestRun` 契约：

| 字段 | 含义与来源 | V2 落点 |
|---|---|---|
| `canonical_generation` | `resolve_published_history` 返回的 canonical history generation | `data_snapshot.canonical_generation` |
| `local_overlay_latest_date` | 本地可信 enriched overlay 的最新日期 | `data_snapshot.local_overlay_latest_date` |
| `data_cutoff` | 本次回测允许读取的最后交易日 | `data_snapshot.data_cutoff` |
| `adjustment_mode` | 价格复权口径（当前主链为前复权） | `data_snapshot.adjustment_mode` |
| `adjustment_generation` | 复权事件/因子所依赖的发布 generation；无法证明时为 `null` 并产生 warning | `data_snapshot.adjustment_generation` |
| `universe_definition` | 股票池来源及组成规则 | `universe_snapshot.definition` |
| `universe_as_of` | 股票池成立时点；当前集合无法证明历史时点时为 `null` | `universe_snapshot.as_of` |
| `strategy_hash` | 冻结后的策略定义哈希 | `subject.hash` |
| `engine_version` | 回测引擎契约版本 | `engine_version` |
| `metric_version` | 指标契约版本 | `metric_context.version` |
| `random_seed` | 随机稳健性分析 seed | `random_seed` |

`canonical_generation` 是已发布全历史快照的不可变版本号；`adjustment_generation` 是复权事件/因子来源的不可变版本号，两者不得混用。当前股票池无法证明 point-in-time 时，结果必须显示幸存者偏差警告，不得静默视为历史股票池。

> 落地：`app/backtest/provenance.py::build_data_snapshot` 输出上表全部字段（`universe_definition`/`universe_as_of` 平铺在 `data_snapshot` 内，语义同 `universe_snapshot`），并计算 `snapshot_hash` 供比较/复跑检测数据漂移；显式标的列表时 `universe_as_of = end` 且无偏差警告，全市场池时固定追加 `survivorship_bias` 警告。测试 `tests/backtest/test_provenance.py`。

### P0-6 双引擎语义分裂（✅ 已隔离）

旧 vectorbt 信号回测与主 Polars/NumPy 策略引擎存在成交、统计和持久化差异。旧入口必须明确标记 legacy，停止新增消费者；主策略、因子、组合和参数网格统一以 `app/backtest/*` 为权威实现。

> 落地：旧 vectorbt 入口 `POST /api/backtest/run` 响应固定携带 `legacy_vectorbt_engine` 警告；主策略、因子、组合和参数网格统一走 `app/backtest/*`。

## 5. 实验闭环设计契约

### 5.1 BacktestRun

完整 Run 是一等领域对象：

```text
run_id
kind                 # strategy | factor | composite
created_at
status
subject              # {id, name, hash}
config
data_snapshot
universe_snapshot
benchmark
cost_model
metric_context       # 含 version
random_seed
engine_version
stats
equity_curve
drawdown_curve
benchmark_curve
trades
per_symbol_stats
factor_result        # kind=factor 时使用
warnings
methodology_context
favorite
label
```

> 实现差异（以 `run_store.BacktestRun` 为准）：`universe_snapshot` 未作为独立顶层字段，`universe_definition`/`universe_as_of` 平铺在 `data_snapshot` 内；`methodology_context` 仅存在于 API 响应载荷、不持久化进 Run（模型 `extra="ignore"`）；实现额外含 `schema_version` 与 `source_run_id`（复跑溯源）。

策略与组合策略使用 `stats/equity_curve/trades` 主结果；因子 Run 使用共享元数据加 `factor_result`（IC、分层、多空），不伪造交易字段。参数网格继续作为 `GridExperiment` 独立持久化，24/36 个场景不自动膨胀为 Run；只有用户选择“用此参数回测”后产生一个策略 Run。

BacktestRun 取代现有 run card，成为唯一持久化契约。旧 `data/research/run_cards/*.json` 采用**只读惰性迁移**（落地决策，与"一次性搬文件"不同）：列表/读取时合入结果并标记 `warnings=["legacy_run_card"]`，仍可查看摘要但不能伪装成完整 Run；仅当用户 PATCH 收藏/标签时才把该卡固化为新契约文件（原文件不动）；DELETE 对旧卡拒绝（403）。同 run_id 冲突时以新契约文件为准。回测域（`strategy`/`factor`/`composite`）所有写入方只写 BacktestRun；`run_cards` 目录仍承接非回测域研究卡（AI 池研究 `pool_backtest_*`、定时研究 `scheduled_research`），kind 不在 `RUN_KINDS` 内、不进运行历史。

约束：

- Run 存储于 `data/research/backtest_runs/{run_id}.json`，索引独立原子写；
- 单 Run 序列化后上限 20 MiB，超限时明确失败，不静默截断交易或曲线；
- 不自动删除 Run；列表分页，删除必须由用户显式触发，收藏仅影响筛选；
- Run 文件必须原子写；
- `run_id` 必须经过白名单校验，禁止路径穿越；
- JSON 非有限值统一写 `null`；
- Run 是不可变事实，只有 `favorite`/`label` 允许作为独立元数据更新；
- 写入失败不得把回测伪装成成功；
- Run 不得包含密钥、账户凭证或用户目录绝对路径。

### 5.2 API

```text
GET     /api/backtest/runs
GET     /api/backtest/runs/{run_id}
POST    /api/backtest/runs/compare
POST    /api/backtest/runs/{run_id}/rerun
GET     /api/backtest/runs/{run_id}/export?format=json|csv
PATCH   /api/backtest/runs/{run_id}
DELETE  /api/backtest/runs/{run_id}
```

> 实现：七个端点全部落地于 `app/api/backtest.py`；导出的查询参数名为 `fmt=json|csv`（非 `format`）。

`PATCH` 请求体只接受 `favorite` 和 `label`；包含其他字段一律拒绝，对应 §5.1 的不可变事实约束。`DELETE` 仅删除明确指定的 Run，不级联删除策略、网格实验或其他研究事实。

比较接口必须返回：

- 可比性检查：区间、股票池、基准、数据版本、费用模型、策略版本；
- 核心指标值和相对基准 Run 的 delta；
- 曲线；
- 配置差异；
- 共同/新增/消失交易摘要；
- 不可比时的结构化 warning。

### 5.3 前端

- 运行历史抽屉：搜索、标签、收藏、取回、复跑、导出；
- 支持选择 2～4 个 Run；
- 指标差异表；
- 净值、累计超额、回撤、滚动指标叠加；
- 配置差异及数据口径警告；
- 参数网格任意场景可“一键带回策略回测”；
- Run 结果不会因修改当前表单而消失。

### 5.4 已落地状态与关键工程决策（2026-08-19）

**已按契约落地**（`app/backtest/run_store.py` + `app/api/backtest.py` + `frontend/src/pages/backtest/RunHistoryPanel.tsx`，测试 `tests/backtest/test_run_store.py`、`tests/api/test_run_store_api.py`）：

- §5.1 全部约束：20 MiB 上限、原子写（tmp + fsync + link/replace）、run_id 白名单正则防路径穿越、非有限值写 `null`、不可变事实仅 `favorite`/`label` 可变（`PATCH` `extra=forbid`，其余字段 422）、删除仅显式指定文件且旧卡 403。
- §5.2 全部七个端点。比较接口实际返回 runs 摘要、标量指标矩阵、归一化曲线、结构化 `compare.*` 警告，以及相对首个 Run 的递归配置差异和共同/新增/消失交易摘要；配置差异上限 200 条、交易样本每类上限 20 条，计数保持完整。前端提供对应差异区块与单 Run 完整配置/指标详情。
- §5.3：运行历史页（独立 Tab）支持 kind/收藏/关键词筛选、分页、详情取回、JSON/CSV 导出、复跑、删除、2~4 Run 指标矩阵（首列为基线，其余列显示 Δ）与归一化净值曲线；Run 结果持久化在服务端，不随表单修改丢失。

**关键工程决策记录**：

1. **BacktestRun 唯一持久化**：新运行只写 `data/research/backtest_runs/{run_id}.json`；同 run_id 已存在时拒绝覆盖，SSE 重连/多订阅者重复组装的同一事实（仅 `created_at` 不同）幂等返回已存在 Run，载荷不一致则拒绝并记日志。
2. **旧 run_card 只读**：见 §5.1 修订段；`LegacyRunCardReadOnly` 语义 fail-closed。
3. **MetricContext / risk_free_rate**：回测请求可显式传年化无风险利率（默认 0），进入 `metric_context` 并统一作用于 Sharpe/Sortino/相对指标与稳健性分析；比较时频率、年化周期、ddof 或无风险口径不同即触发 `compare.metric_context_mismatch`。
4. **基准与可比性告警**：基准可选上证/沪深300/中证500/中证1000；比较时对区间、股票池 hash、基准、canonical generation、指标版本、指标口径、成本模型、引擎版本、曲线语义九类差异输出 `compare.*` 结构化警告，前端去前缀直译展示。
5. **参数网格回填**：网格实验独立持久化（`GridExperiment`）；前端"回填策略"将场景参数带回策略回测表单，由用户显式发起一次新回测生成 Run，网格不自动膨胀为 Run。
6. **打印与离线研究报告**：结果页"打印 / PDF"按钮走浏览器打印（`@media print` + `.no-print` 隐藏交互控件）；策略、因子和运行历史均可由 `backtestReport.ts` 导出无脚本、无外链的自包含 HTML 报告。
7. **持久化失败显式提示**：Run 落盘失败时回测响应追加 `persistence_failed: 回测已完成，但完整运行记录未能写入运行历史` 警告，绝不把回测伪装成"已进入运行历史"；复跑保存失败直接 500。
8. **复跑数据漂移告警**：复跑生成新 `run_id` 并记录 `source_run_id`，不改动原 Run；新旧 `snapshot_hash` 不一致时追加 `rerun_data_snapshot_changed` 警告，明确与原 Run 不可直接比较。

## 6. 专业报告目标

结果页按五组组织：

1. **收益**：累计、年化、月度/年度、累计超额、月度热力图；
2. **风险**：波动率、Sharpe、Sortino、Calmar、最大回撤、回撤持续时间、VaR/CVaR、尾部风险；
3. **基准相对**：Alpha、Beta、Information Ratio、Tracking Error、上下行捕获率；
4. **交易质量**：胜率、Payoff Ratio、Profit Factor、Expectancy、连胜/连亏、持仓周期、MAE/MFE、退出原因贡献；
5. **执行质量**：总费用、滑点、换手率、成交阻塞、平均/最大仓位、pending exit。

图表至少包括：

- 策略与基准净值；
- 累计超额收益；
- 水下回撤及持续时间；
- 月度收益热力图；
- 60/120 日滚动收益、波动率、Sharpe；
- 仓位/暴露/换手/成本；
- 退出原因和标的贡献。

导出目标：JSON、CSV，以及可直接浏览器打印为 PDF 的独立 HTML 研究报告。

> 落地状态（2026-08-19）：五组指标中，收益/风险/基准相对/执行质量已由 `metrics.py::performance_metrics`（Sortino、Calmar、Omega、尾部比率、Ulcer、VaR/CVaR、连胜连亏、持仓时长、暴露度等）与 `relative_performance_metrics`（Alpha/Beta/IR/TE/上下行捕获）统一产出，前端 `ProfessionalDiagnostics` 分组展示；交易质量含 Payoff/Profit Factor/Expectancy，`engine.py::TradeRecord` 还按成交口径记录持仓期 `mae_pct`/`mfe_pct`，前端聚合展示有效样本、均值与极值（`tests/backtest/test_trade_excursions.py` 锁定入/退出边界）。图表已落地：净值+基准+回撤（`StrategyNavChart`）、60/120 日滚动收益/波动/Sharpe、月度收益热图、水下回撤与连续水下天数（`UnderwaterDurationChart`）、暴露/当日换手/累计估算成本执行时间线（`ExecutionTimelineChart`，成本按固定成本模型 fees_pct + slippage_bps/10000 × 名义金额估算并与 `cost_breakdown` 总额校验）、退出原因与标的贡献图（`ContributionChart`，基于 trades 聚合 count/total_pnl/avg_pnl/win_rate，Top 10 + 其余合并）。导出已落地 JSON（完整 Run）与 CSV（trades / 因子 group_stats）；`backtestReport.ts` 生成无脚本、无外链、内联 CSS/SVG 的自包含 HTML 文件，策略、因子和运行历史均可离线下载，结果页仍支持 `window.print()`。

> 交易归因补充（2026-08-19）：`TradeAttributionPanel` 展示交易窗口、按当前行业分类、相对等权已执行交易样本的 Brinson-Fachler 归因；输入不足、行业映射不足或资金覆盖不足时 fail-closed，只显示结构化原因和覆盖度，不生成分解数字。行业映射非 point-in-time，Fama-French 在无冻结可审计本地因子序列时显式 unavailable，绝不伪造代理结果。交易明细支持代码/名称、盈亏方向、退出原因、分页的本地筛选。

## 7. 真正的样本外与 Walk-Forward

严格流程：

```text
训练窗口优化 → 冻结参数 → 样本外运行
窗口滚动 → 再优化 → 再冻结 → 下一样本外窗口
```

输出必须包含：

- 每个 fold 的训练/OOS 区间；
- 每个 fold 的冻结参数；
- OOS 拼接净值；
- 参数漂移；
- 正收益 fold 比例、最差 fold；
- 训练到 OOS 的退化幅度；
- 参数邻域稳定性。

原有顺序切段汇总继续保留，但命名为“分段稳定性”，不再冒充完整 Walk-Forward。

> 落地状态（2026-08-19）：**已实现（`/strategy/robustness`）**。`app/backtest/robustness.py::run_walk_forward` 把请求区间切成 expanding 训练窗 + 互不重叠的 OOS 窗（区间切成 n+1 份，首份为初始训练窗，之后每份依次为各折 OOS；每折 OOS ≥30 天，请求折数放不下时自动收缩，连 1 折都放不下时返回结构化 warning 与空折，不伪造）。每折先在训练窗对候选集运行并**仅按训练期有限 Sharpe** 选参（平局按候选顺序稳定 tie-break，baseline 最先；全部不可计算时确定性回退 baseline），冻结 winner 后仅在 OOS 窗运行一次——`select_walk_forward_candidate` 的输入只有训练窗口结果，OOS 指标结构上无法进入选择。输出含每折 train/OOS 日期、候选数、selected_params、train/oos stats、degradation、折内归一 OOS 曲线，以及拼接 OOS normalized equity（逐折首点归一链式相乘）、正收益折比例、最差折、平均退化与参数漂移。候选空间 = baseline + `parameter_perturbations` 生成的一次单参数 ± 邻域，上限 `1 + 2 × max_perturbed_params`（≤17），**为局部邻域而非全局优化**。前端 `StrategyRobustnessPanel` 新增"Walk-Forward 样本外"专业区（训练→冻结→OOS 说明、拼接曲线、折表、退化与参数漂移）。原同参数顺序切段已改名 `segment_stability`（"分段稳定性"），不再冒充 Walk-Forward。测试 `tests/backtest/test_walk_forward.py`（无泄漏/冻结/拼接/短区间/有界候选）、`tests/api/test_strategy_robustness_api.py`（响应契约与持久化一致性）、`test_robustness_windows.py`（分段窗口边界）。

## 8. 分阶段实施顺序

1. 因子成本和换手（✅）；
2. 指标统一（✅）；
3. Profit Factor/Payoff Ratio（✅）；
4. 稳健性 seed（✅）；
5. 方法论和幸存者偏差警告（✅）；
6. 数据快照元数据（✅）；
7. legacy 引擎隔离（✅）。

### V2 实验闭环
1. BacktestRun 持久化（✅）；
2. Run 列表、读取、比较、复跑和导出 API（✅，比较缺口见 §12）；
3. 运行历史、收藏和标签（✅）；
4. 2～4 Run A/B 对比（✅）；
5. 可选基准及相对指标（✅）；
6. 参数网格回填（✅）。

### V3 专业报告


1. 高级收益、风险、相对基准和交易指标（✅，含持仓期 MAE/MFE）；
2. 月度、滚动、水下持续、暴露、换手和成本图表（✅ 已落地，成本为固定成本模型估算并与 cost_breakdown 校验）；
3. 交易筛选、MAE/MFE、贡献分析和交易窗口行业归因（✅；交易明细支持代码/名称、盈亏与退出原因筛选及分页；Brinson-Fachler 基于当前行业映射和等权交易基准，Fama-French 无冻结因子序列时 fail-closed）；
4. 真正 IS/OOS 与 Walk-Forward（✅ 训练选参→冻结→OOS 已落地；候选为局部单参数邻域，非全局优化）；
5. 参数热力图、稳健邻域和 Pareto 前沿（✅ 热力图已落地；收益–回撤散点上叠加严格三目标 Pareto 分层，第一层为非支配解，独立于目标得分排序）；
6. 因子任务体验统一（✅ 与策略共用服务端任务语义：SSE 进度回放、显式取消、刷新/路由重连和 `BacktestRun` 持久化）；
7. HTML/打印报告（✅ 自包含离线 HTML 下载与页内打印均已落地）。

## 9. 验收标准

### 正确性

- 相同配置、相同数据版本、相同 seed 重复运行时，除 `run_id`、`created_at`、`elapsed_ms` 等运行元数据外，`stats`、`equity_curve`、`drawdown_curve`、`trades` 逐字段一致；
- 因子费用变化对净值产生可预测影响；
- 周/月频指标按真实频率年化；
- Payoff Ratio 与 Profit Factor 有独立数学测试；
- full 模式样本收益不得标成账户净值；
- 未冻结股票池必须有显著警告；
- `PATCH` 修改 `favorite`/`label` 以外字段会被拒绝，且 Run 内容保持不变。

### 可复现性

- 页面刷新和服务重启后能取回完整 Run；
- Run 可导出并包含配置、数据、策略、指标版本；
- 任一图表和交易明细都能追溯到唯一 Run；
- 复跑使用原配置，并显式展示数据版本是否已变化。

### 对比

- 可选择 2～4 个 Run；
- 不同区间、股票池、基准、数据版本会触发可比性警告；
- 指标 delta、曲线叠加和配置差异一致；
- 参数网格场景可以无损带回策略表单。

### 专业报告

- 收益、风险、相对基准、交易、执行五组指标齐全；
- 月度热力图、滚动指标、暴露和成本图可用；
- IS/OOS 不混算，OOS 拼接净值只使用冻结参数；
- HTML 报告不依赖当前页面状态，可打印为 PDF。

### 质量门槛

- 新契约具备后端单元/API 测试；
- 任务取消、SSE 重连、Run 持久化和导出具备边界测试；
- 前端类型检查、生产构建通过；
- 浏览器验证“运行 → 保存 → 对比 → 导出 → 复跑”主路径；
- 不修改 `data/` 下已有用户事实文件，测试使用隔离临时目录。

## 10. 竞品依据

- [QuantConnect Backtesting Results](https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/results)
- [QuantConnect Backtest Report](https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/report)
- [QuantConnect Optimization Results](https://www.quantconnect.com/docs/v2/cloud-platform/optimization/results)
- [QuantConnect Walk Forward Optimization](https://www.quantconnect.com/docs/v2/writing-algorithms/optimization/walk-forward-optimization)
- [QuantConnect Backtest Analysis](https://www.quantconnect.com/docs/v2/research-environment/meta-analysis/backtest-analysis)
- [TradingView Performance Summary](https://www.tradingview.com/support/solutions/43000681683-performance-summary-tab/)
- [TradingView Export Strategy Data](https://www.tradingview.com/support/solutions/43000613680-how-to-export-strategy-data/)
- [TradingView Deep Backtesting](https://www.tradingview.com/support/solutions/43000666265-how-deep-backtesting-works/)
- [vectorbt Features](https://vectorbt.dev/getting-started/features/)
- [vectorbt Portfolio API](https://vectorbt.dev/api/portfolio/base/)
- [vectorbt Trades API](https://vectorbt.dev/api/portfolio/trades/)
- [Backtrader Analyzers](https://www.backtrader.com/docu/analyzers-reference/)
- [Backtrader Benchmarking](https://www.backtrader.com/docu/observer-benchmark/benchmarking/)
- [聚宽 API 文档](https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf)

## 11. 实施进度

| 阶段 | 状态 | 证据 |
|---|---|---|
| V1 可信度修复 | ✅ 已完成 | `tests/backtest/test_factor_costs.py`、`test_metrics.py`、`test_provenance.py`、`test_strategy_backtest_correctness.py`（因子成本、MetricContext、Profit/Payoff、seed、快照元数据、方法论告警、legacy 隔离） |
| V2 实验闭环 | ✅ 已完成 | `app/backtest/run_store.py`、`tests/backtest/test_run_store.py`（27 用例）、`tests/api/test_run_store_api.py`（21 用例）、前端 `RunHistoryPanel.tsx` |
| V3 专业报告 | ◐ 部分完成 | 专业诊断/滚动/月度热图/参数热力图/严格收益–夏普–回撤 Pareto 分层/MAE-MFE/交易筛选/交易窗口 Brinson-Fachler 行业归因/独立 HTML 报告/页内打印/严格 Walk-Forward/因子 SSE 任务（进度回放、取消、刷新重连、Run 持久化）已落地；对应 `tests/backtest/test_attribution_report.py`、`test_trade_excursions.py`、`test_walk_forward.py`、`test_parameter_grid.py`、`tests/api/test_backtest_factors.py`、`frontend/src/lib/backtestReport.test.ts`、`factorBacktestTask.test.ts`。参数前沿仍只覆盖受限候选空间，见 §12 |
| V4 可信度增强（2026-08-20） | ✅ 已完成 | 量能参与率约束与容量统计（`test_volume_participation.py` 17）、成本敏感性（`test_cost_sensitivity.py` 14）、PSR+交易级 bootstrap 带（`test_psr_and_band.py` 12）、上市天数门控与偏差拆分（`test_universe_gating.py` 8）、市场状态分桶（`test_regime_breakdown.py` 10）、本地风格因子 SMB/UMD/LMV（`test_style_factors.py` 13）、成交可达性诊断（`test_fill_reachability.py` 9）；API 接线与 SSE 透传（`test_backtest_enhancements_api.py` 21）；前端 TrustDiagnostics/Regime/CostSensitivity/StyleAttribution 面板、简单模式、预检、指标解释（`trustDiagnosticsCore.test.ts` 7、`MetricExplainer.test.ts`、`runPreflight.test.ts`）；寻优场景一键固化为 Run。真实数据验证：PSR=0.058（-65.6% 策略）、容量 99.7x、门控滤 55 只次新股、成本敏感性单调、regime 四桶（熊市动荡 Sharpe -1.63）、风格归因 UMD β=-1.43（t=-2.35）、fill-reachability headroom p50=2.5 |
| 完整回归与浏览器验收（2026-08-20 复验） | ✅ 已完成（回测相关集合） | 后端 `tests/backtest` 594 项 + 回测 API 域 66 项全部通过；前端 tsc exit=0、Vite build 通过、Bun 断言（trustDiagnosticsCore 7/7、MetricExplainer、runPreflight、既有 18/18 等）通过。浏览器已验证：带参与率/上市天数门控的 SSE 回测全链路（TrustDiagnostics 渲染容量 ≈103x、PSR 25.4%、门控统计）、市场状态面板 3 个月区间的 fail-closed 样本不足提示、简单/专业模式切换、预检 3 项警告。未运行全仓 pytest。 |

每完成一个阶段，必须更新本节及对应验收证据；不得以 scaffold、占位接口或仅有 UI 的假实现标记完成。

## 12. 当前边界与未实现清单（2026-08-19）

以下为当前实现的真实边界与尚未实现的能力，任何文档、UI 文案或对外说明不得夸大：

1. **Walk-Forward 的优化边界**：严格 IS/OOS（训练选参→冻结→OOS、拼接净值、退化与参数漂移）已实现；但候选空间仅为 baseline + 单参数 ± 扰动局部邻域（≤ `1 + 2 × max_perturbed_params` 个），不是网格/全局优化，训练期选参只在该局部邻域内进行。
2. **参数前沿的样本边界**：收益–夏普–回撤 Pareto 分层是严格非支配判定，但只覆盖当前受限候选网格；不得把它宣传成全局最优参数证明。
3. **成交量约束已落地参与率上限，仍非盘口冲击模型**：撮合支持 `max_participation_pct`（单笔买入 ≤ min(当日量, N 日均量) × p%），截断计入 `buy_volume_cap` 阻塞原因并输出容量统计（利用率分位、`est_capacity_multiple` 线性外推近似，非精确容量解）；仍不模拟盘口深度、部分成交与价格冲击。
4. **历史时点股票池**：全市场池无法证明 point-in-time。上市天数门控（`min_listed_days`，provider `get_stock_reference_flags` 的 `ssdate`）已可显式过滤次新股，provenance 侧幸存者偏差拆分为 `delisting_bias`（退市标的历史缺失，本地源无法回补，实测 164 只退市标的仅 3 只有 tdx 日 K 历史）与 `listing_age_bias` 两条独立警告；退市偏差无法由面板层修复，告警必须保留。
5. **撮合引擎仍无 intraday 数据**：`close_t` / `open_t+1` 两档口径，不支持盘中触发。新增的成交可达性诊断（`/runs/{run_id}/fill-reachability`，分钟级价格带成交额 vs 交易名义额的 headroom 抽查）只是事后诊断口径，不构成盘中撮合能力。
6. **全量独立候选模式**：它评估每笔候选的独立交易质量，候选样本收益曲线按退出事件日等权复利；不是资金受约束的账户净值，故年化、风险调整、基准与相对绩效指标刻意不可用。
7. **策略寻优 V1 不是全局最优**：`/backtest` 策略寻优 tab 对策略 × 股票池 × 持仓周期 × 撮合做笛卡尔展开（默认上限 120，超出确定性抽样），在最近 N 年冻结窗口上训练期打分、留出期确认，并报告 DSR/PBO。它不搜索策略参数、不调用 Optuna、不写入策略池；推荐仅表示留出收益为正且成交数达标。设计见 `STRATEGY_SEARCH_DESIGN.md`。
8. **风格归因无 HML 价值因子**：本地风格因子为面板内自建 SMB/UMD/LMV 三因子（三分位、截面 ≥100，`factor_version` 为构造规格指纹，数据版本由快照补充），不含价值因子——本地面板无账面市值/ROE 历史序列，待财务数据接入，绝不伪造代理；OLS 标准误未做 Newey-West 修正。Fama-French 正式接口维持不可用。
9. **市场状态分桶与 PSR 的口径边界**：regime 分桶的波动阈值用基准全样本中位数（事后口径，含轻度前视，仅作分组解释不作交易信号）；PSR 只校正收益分布形态与样本量，不校正数据窥探/多重试验（后者由寻优的 DSR 承担）；交易级 bootstrap 净值带是顺序无关的单仓位复利诊断，不是账户净值。

**定位声明**：本系统输出为历史研究与分析，不构成荐股、投资建议或下单指令；所有结果须结合方法论告警（幸存者偏差、数据快照、口径差异）解读。
