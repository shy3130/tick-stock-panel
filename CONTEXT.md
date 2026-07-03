# CONTEXT — 项目词汇表（glossary）

> 本文件只收录项目专用术语的**精确定义**，不含实现细节、不当规格书用。

## 策略 DSL 领域

**策略 DSL（Strategy DSL）**
声明式 JSON 策略定义。一个策略一个 JSON，含 `meta`/`basic_filter`/`entry`/`exit`/`scoring`/`risk`。取代原先"Python 文件 + `filter()` 函数 + `importlib.exec`"的策略形态。
_避免_：把 DSL 说成"策略脚本"/"策略代码"——它不是可执行代码，是声明式数据。

**Expression IR（表达式中间表示）**
DSL 解析后的**语言无关 AST**，是跨语言唯一契约。节点封闭集合：`col`/`lit`/`param`/`arith`/`cmp`/`logic`/`window`。各语言后端各自实现"IR → 本地算子"的 compiler。
_避免_：把 IR 与 DSL 混为一谈——DSL 是用户/AI 书写面，IR 是内部契约面。

**窗口/状态算子（Window Operator）**
IR 中 `window` 节点承载的封闭算子集合（`rolling_*`/`shift`/`pct_change`/`n_day_high/low`/`cross_up/down`/`consecutive_true`/`cs_rank`/`cs_qcut`）。均在 `symbol` 分组内、按 `(symbol,date)` 排序求值。**不含**递归类（EMA/MACD/KDJ/RSI），后者走 indicators 流水线预计算。见 [ADR-0001](docs/adr/0001-dsl-keeps-full-window-operator-catalog.md)。

**预计算列（Enriched Column）**
indicators 流水线盘后落盘的指标/信号列（`signal_*`、`ma*`、`consecutive_limit_ups` 等）。策略 DSL 可直接引用其列名。是与"窗口算子内联计算"并存的另一取数途径。

**黄金测试（Golden Test）**
一份固定 fixture 面板 + 覆盖全部 IR 节点/算子的 DSL 用例 + 期望输出。所有语言后端 compiler 跑同一套并比对，用于防止跨语言语义漂移。

## 数据源领域

**上游源（Upstream Source）**
panel provider 层直连的具体数据来源。分两类：
- **本地核心源**（自有局域网设施，承载核心行情）：`engine-data`(HTTP，日 K 主源)、`fstore`(PG，元数据/财务)、`tdx`(实时)。
- **第三方补充源**：`waizao/wz`(HTTP，token 鉴权)，**仅**供涨停梯队/情绪面/板块/名称等补充数据；**核心日 K/实时永不回退到它**。见 [ADR-0003](docs/adr/0003-waizao-supplementary-only.md)。
取代原 TickFlow 付费 SDK。

**Provider**
`data_providers` 抽象层对业务暴露的统一取数接口（`get_daily`/`get_realtime`/…）+ `capabilities`。业务层只认 Provider，不认具体上游源。

**capability**
Provider 声明的能力开关（`daily`/`realtime`/`depth`/`universes`…）。业务入口前必须检查；缺口能力降级返回空。`depth`（5 档盘口）为永久缺口。

**Manager 链式 fallback**
每个 capability 一条有序上游源链，逐源尝试、首个非空即返回、异常降级下一个。编排策略对齐 `../fquant`(Go) 的 `Manager`。

## 交易复盘领域（Vibe-Trading 迁移候选 C1）

**交易流水复盘（Trade Journal）**
对用户导入的**真实券商成交流水**做**事实性**诊断：FIFO 配对成 roundtrip，算 PnL/胜率/持仓天数/盈亏比/回撤，并识别行为偏差（处置效应/过度交易/追涨/锚定）。输入是「我实际怎么交易的」，输出是「我实际在哪亏钱」。是 Track B 的**第一里程碑**。
_避免_：与 [[Shadow Account]] 混为一谈——Trade Journal 只描述已发生的事实，不生成任何假设组合。行为诊断第一阶段用**纯统计规则**，不把真实流水送外部 LLM（Hybrid 叙事仅送聚合数字，后续 opt-in）。

**Roundtrip 台账（Roundtrip Ledger）**
Trade Journal 的核心数据结构：一次"建仓→清仓"的完整回合，含代码/建仓日/清仓日/买入均价/卖出均价/数量/持仓天数/费用/已实现盈亏。**由 FIFO 配对归一化后的 raw fills（原始逐笔成交）产生**，是唯一事实源。
_避免_：把券商预配对产物当事实源。同花顺投资账本导出的「已清仓」sheet 虽是现成 roundtrip，但那是券商专有黑盒配对，只作**对拍 oracle**（验证我方 FIFO 配得对不对，套路同 raw-reconstruct 用 fstore 当 oracle），不作数据源——否则重蹈"依赖 per-broker 专有格式"的脆弱性。

**导入解析约定（同花顺投资账本 / 银河样本）**
真实导出是**多 sheet xlsx**（持仓数据 / 已清仓 / 交易记录），非单张 CSV。解析事实源为「交易记录」sheet，须：① 按 `交易类别 ∈ {买入,卖出,…}` 过滤掉非交易行（银行转证券等，代码为空）；② `发生金额` 符号买入负/卖出正，`费用` 单列；③ 代码 A股/港股混合（5 位前导零=HK 如 02577/06088，6 位=A 股），归一复用 P6 的 HK/A 判定。通用列映射（[[Q4 决策]] 方案 C）之上，同花顺投资账本存一份列名预设。

**影子账户（Shadow Account）**
从用户盈利 roundtrip 中抽取 if-then 个人规则，把规则**回放为一个假设组合**，与真实交易做 delta-PnL 归因。输入是「我的交易 + 抽取的规则集」，输出是「一个更自律版本的我会怎么做」的**反事实**。是 Trade Journal 之后**显式延后**的独立后续项（成本高：需规则抽取 + codegen + 第二回测引擎），是否启动取决于 Trade Journal 的行为诊断是否足够好到值得回放。
_避免_：把它当作 Trade Journal 的"Phase 3"顺带交付——它是独立立项决策，不是同一 bullet 下的隐藏后续。
