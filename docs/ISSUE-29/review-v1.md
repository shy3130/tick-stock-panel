# ISSUE-29 review-v1：对 plan-v1 的独立审查记录

> 审查对象：[plan-v1.md](plan-v1.md)（基线 `7bf2982`）。
> 审查来源：独立 coding review（ReviewZuoyiPlan1），本文件忠实转写其全部 finding 与结论，未做辩护或改写。
> 日期：2026-08-28 · 对应 Issue：[wf2311/fm-workbench#29](https://github.com/wf2311/fm-workbench/issues/29)
文档导航：[README.md](README.md) · [feasibility.md](feasibility.md) · [plan-v1.md](plan-v1.md) · [plan-v2.md](plan-v2.md)
## 总体结论

**verdict：`incorrect`（拒绝当前 plan-v1）。** confidence：0.98。

审查理由（原文转写）：六臂入场和共同观察期未冻结，复权成交与一字跌停可卖性又与 sealed-reader 的真实列/接口冲突，会使回测统计和执行证据不可实现或失真。先完成全部 P1 修订并把状态机、请求模型和统计口径固定后，再进入实施。

## Findings

P1 = blocker 级；P2 = 必须修复级。共 9 项：6×P1、3×P2。

### R1（P1，confidence 0.97）为一字跌停可卖性冻结可得的限跌停输入

方案要求一字跌停进入 pending_exit（plan-v1 §4），但 sealed daily 的持久列只有 `consecutive_limit_downs`，不含 `signal_limit_down`；现有引擎的可卖性正是依赖该信号再配合 OHLC 同价判定（`backtest/engine.py:930-960`）。该信号若重算还依赖 instruments 中的 ST 名称/板块规则（`indicators/pipeline.py:595-668`），而方案又限定只能读单一 canonical generation、raw 仅作 evidence。因而按该契约既无法正确识别 ST 一字跌停，也无法 fail-closed 地实现所承诺的阻塞退出，结果会把不可卖日当成可在 open 成交，扭曲六臂收益和删失。最小修正是明确一个同 generation、PIT 的限跌停可卖性字段/reader 输入并列入 required columns 与 provenance；若没有该输入，整个 evaluate 必须 unavailable，而不能仅凭 OHLC 猜测。

### R2（P1，confidence 0.99）冻结 common entry 的入场触发、成交日与去重规则

六臂要求共享 `common_entry_set`（plan-v1 §5），但方案没有定义任何入场事件：上涨状态只是计算防守线的资格，未说明是状态首次成立、每个满足状态的交易日、某个外部信号还是调用方提供的日期触发入场；也没有规定信号日到实际入场 open 的映射、入场不可达时的处理及持仓期间重复信号的去重。这使相同 symbol/date 请求可产生不同样本数、持仓段和 IS/OOS 归属，既不能证明六臂公平，也无法执行 Issue 的 common-entry 验收。最小修正是在定义和请求模型中冻结唯一的 entry predicate、T 到 execution-day 的规则、不可买/缺 bar 的 censor 码，以及"一标的一段持仓内不再入场"的规则，并把由此得到的 entry IDs 输出到每臂结果。

### R3（P1，confidence 0.96）定义按交易日对齐的组合 NAV 与右删失统计口径

方案规定"成本后再聚合等权持仓段收益"后复用年化收益、Sharpe 和最大回撤（plan-v1 §5），但没有把不同进/出场日的段转换为同一交易日的组合收益、现金权重和同时持仓权重。现有 `backtest/metrics.py:790-910` 的年化/Sharpe/回撤都以等频、时序有序的 returns 序列为输入；直接对持仓段收益等权会把 1 天和 100 天的段当作同一个周期，且每臂离场不同会得到不同的观测时间轴，统计值和 bootstrap CI 因此没有确定含义。最小修正是冻结每日 mark-to-market NAV（含未平仓按 close 估值、现金、重叠仓位权重、成本扣除）和所有臂的共同 calendar，或删去这些路径指标、仅报告预先定义且删失正确的逐段统计；同时规定 pending/right-censored 段是否及如何进入每个统计量。

### R4（P2，confidence 0.99）冻结卖飞率和破位后跌幅的完整观测窗口

诊断指标无法按当前定义确定计算：卖飞率中的"离场时 rolling-max"没有 lookback 长度、是否包含离场日或与持仓期峰值的关系，破位后下跌深度使用未给出取值集合或默认值的 `M`（plan-v1 §5）。不同实现会在同一 exits 上给出不同结果，而且临近样本末端的事件会有不同删失数，违反要求的结构化、可审计诊断。最小修正是将两个 lookback/forward horizon、端点包含规则、使用 adjusted close 的原因和 horizon 不完整时的单独 censor/分母规则固化为常量与 provenance。

### R5（P1，confidence 0.94）分离复权研究价格与真实成交价格的执行账本

方案规定所有价格计算只能使用前复权 OHLC、`raw_*` 只能作为 evidence/provenance 且不得混算（plan-v1 §3），同时又要求跳空按"实际 open"成交并输出实际执行价（plan-v1 §4）。历史前复权 open 并非该日可成交的原始报价；把它作为实际成交会使交易账本不真实，而改用 raw_open 又直接违反本条的禁止混算，尤其在除权前后会改变成本后段收益。最小修正是明确两套序列：复权 OHLC 仅用于形态/防守线/经济收益归一化，raw OHLC（及企业行动后的持仓数量或等价调整规则）用于可成交性和 execution evidence；或者将该研究明确为 adjusted-price hypothetical、删除"实际成交价/真实可达性"的承诺。

### R6（P1，confidence 0.97）为买入持有和各离场臂冻结共同的结果观察终点

buy-and-hold 被定义为"右删失"（plan-v1 §5），其余五臂又按各自触发日退出；方案没有固定每个 entry 的共同评估 horizon，或规定将未平仓头寸以哪个交易日 close 标记及怎样纳入 common OOS。若只保留完整段，会因 buy-and-hold/最后一段天然删失而排除样本；若每臂各自截到退出日，则比较的是不同时间长度的回报。这会引入由离场规则决定的样本/观测期偏差，不能支持 common OOS verdict。最小修正是按 entry ID 预先规定同一固定持有 horizon（不足 horizon 单独删失）或每日 NAV 的统一截至日，并对每臂以相同 calendar 估值。

### R7（P1，confidence 0.95）规定所有可变参数的训练期选择与 OOS 锁定流程

中位线窗口、tie-break、包含关系、破位后收回和 ATR `k` 都允许多个取值（plan-v1 §4、§5），但只有 ATR `k` 被说成"只在训练集选择"，其余变体没有训练截止日、目标指标、并列规则和最终锁定值；evaluate 也未列出这些参数是请求输入、全量报告还是预先选定。仅以"参数选择只能发生在训练/验证数据"不能消除同一 OOS 请求反复选择表现最好变体的路径。最小修正是为所有变体规定单次 IS/validation selection protocol、固定选择结果后才读取 OOS 的不可变 config，或将 v1 收缩为单一默认配置并只把变体另建研究 run。

### R8（P2，confidence 0.98）在方案中给出可验证的 evaluate 请求模型

所谓冻结 API 只明确 `oos_start` 必填并把 symbol/date/参数范围留给"定义约束"（plan-v1 §2），没有列出 `start`、`end`、`symbols`、成本和变体字段，也没有规定 `start < oos_start <= end`、symbol 规范化或请求上限。现有同类 research endpoint 的 Pydantic 模型均直接定义这些字段和交叉校验，例如 `SingleYangEvaluateIn`（`api/research.py:377-385`）与 `MTFDirectionEvaluateIn`（`services/mtf_direction_15m5m.py:245-276`）。因此实现者无法由这份"冻结"文档生成稳定的 `extra="forbid"` 契约，调用者可以选择不同隐含默认值而使运行不可复现。最小修正是把完整请求/响应 schema、默认/允许值、窗口与 split 校验、symbol 去重/上限和 400 与 unavailable 的边界逐项写入方案。

### R9（P2，confidence 0.96）定义持仓后上涨状态失效时的防守位状态转移

上涨状态只定义了截至 T 的成立条件（plan-v1 §4），但没有说明已入场持仓在其后 `close <= MA60` 或 `MA20 < MA60` 时是保留最后有效防守位、停止更新、防守位失效，还是触发另一种退出；又只在"close 创新高"时要求重算。四种实现对同一价格序列会产生不同 exit 和 pending 结果，且"无左一无离场"不能推出上涨状态失效的处理。最小修正是在状态机中加入 `uptrend_lost` 的显式 transition，并规定它对现有 line、后续 break 检测和 evidence 的唯一行为。

## 与本次修订的关系

全部 9 项（P1+P2）已逐条落入 [plan-v2.md](plan-v2.md) 修订；R1–R9 与 v2 章节的映射见 plan-v2 末尾映射表。plan-v1 保留原样作为被拒存档，不回写。
