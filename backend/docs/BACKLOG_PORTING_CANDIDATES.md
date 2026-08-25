# 移植候选功能 backlog

> 日期：2026-08-14
> 状态：接入状态已审计；P3 保留为候选，需独立产品决策后才实施
> 来源审计：5 个源项目代码级深度对照（PA_Agent / YMOS / go-stock / Vibe-Trading / daily_stock_analysis）
> 权威约束：[`UPSTREAM_FEATURE_PORTING.md`](./UPSTREAM_FEATURE_PORTING.md)、[`PA_AGENT_PORTING_PLAN.md`](./PA_AGENT_PORTING_PLAN.md)、[`YMOS_PORTING_PLAN.md`](./YMOS_PORTING_PLAN.md)

## 红线（实施任何候选前必须确认）

1. 只读本地 DuckDB 是唯一数据主链路；不恢复 TickFlow SDK / tdx-api / PG / HTTP 行情主链
2. 外部 fallback 仅展示级、默认关闭、不写 canonical/enriched/sealed
3. AI 不生成订单、方向、建议价格或执行动作
4. 不引入 PyQt / 桌面 GUI / 券商实盘 / 多公网 fetcher 主链
5. 新代码翻译为当前领域契约，不留兼容 shim 或第二套状态语言

---

## P1 — 高价值，优先实施

| # | 来源 | 功能 | 审计时差距 | 实际落点 | 状态 |
|---|---|---|---|---|---|
| 1 | PA_Agent | JSON 修复算法增强（peek-ahead 引号、智能引号→ASCII、分号分隔符、控制字符转义、花括号深度跟踪） | `ai_structured/parser.py` 仅 4 个修复变体 | `ai_structured/parser.py` | 已接入 |
| 2 | PA_Agent | HTTP 200 body 配额/计费错误检测（gateway 返回 200 但 body 是"积分不足"/"402"） | `ai_provider.py` 只走异常路径 | `ai_provider.py` / `ai_structured/runtime.py` | 已接入 |
| 3 | YMOS | 周期审计：跨所有复盘聚合红旗频率趋势、归因分类分布、策略族胜率，产出变更提案 | `review_job.py` 只有单笔 L0/L1 触发 | `services/trading/cycle_audit.py`、`api/trading_review.py` | 后端已接入 |
| 4 | YMOS | 跨笔统计仪表盘：红旗/归因/策略族/市场/时间段交叉维度 | trade_journal diagnose.py 未与红旗/归因关联 | `services/trading/cycle_audit.py` | 部分接入（缺前端） |
| 5 | Vibe-Trading | Alpha Zoo 因子库（alpha101 / gtja191 / qlib158 / academic，~462 因子） | tickflow 因子极少（factor_zoo.py 基础） | `backtest/factor_zoo.py`、`backtest/factor.py`、`api/backtest.py` | 已接入 |
| 6 | Vibe-Trading | 向量化因子算子（rank / scale / ts_rank / ts_corr / decay_linear / signed_power） | tickflow 因子只用简单表达式 | `backtest/factor_ops.py` | 仅库实现（未接入口） |
| 7 | go-stock | 高级技术指标（Supertrend / KAMA / CMF / Aroon / CMO / ForceIndex / DEMA / TEMA / HullMA 等） | `engine_compat.py` 已有 41 个指标但不含这些 | `indicators/advanced.py` | 仅库实现（15 项，未接入口） |

## P2 — 有价值，第二批实施

| # | 来源 | 功能 | 审计时差距 | 实际落点 | 状态 |
|---|---|---|---|---|---|
| 8 | PA_Agent | Retry 作弊检测（重试后比较 before/after 不可变字段） | `immutable.py` 单次绝对值校验，缺重试前后对比 | `ai_structured/immutable.py`、`ai_structured/runtime.py` | 已接入 |
| 9 | Vibe-Trading | VaR / CVaR / Monte Carlo / Stress Test / EVT 风险分析 | portfolio 有波动/回撤/相关性但缺 VaR 族 | `services/trading/risk_models.py` | 仅库实现（未接入口） |
| 10 | Vibe-Trading | Brinson 归因 + Fama-French 多因子分解 | 无绩效归因分解 | `backtest/attribution.py` | 仅库实现（未接入口） |
| 11 | Vibe-Trading | 量化统计方法（ADF / Cointegration / GARCH / Granger / VIF） | 无时间序列统计检验 | `backtest/quant_stats.py` | 仅库实现（未接入口） |
| 12 | Vibe-Trading | 回测指标增强（per-symbol + per-exit-reason + Calmar / Sortino / IR） | engine.py 有基础指标 | `backtest/metrics.py` | 仅库实现（未接入口） |
| 13 | Vibe-Trading | 4 种组合优化器（等权 / 最小方差 / 最大 Sharpe / 风险平价） | 无组合优化器 | `backtest/portfolio_optimization.py` | 仅库实现（未接入口） |
| 14 | go-stock | K 线形态识别（早晨之星 / 黄昏之星 / 十字星 / 锤子线 / 吞没形态） | analysis_context 有 doji / strong_bull / strong_bear | `indicators/patterns.py` | 仅库实现（未接入口） |
| 15 | go-stock | 本地 OHLCV 异动事件识别（价格突变 / 放量 / 跳空 / 涨跌停） | 无异动事件 | `services/event_stream.py` | 仅库实现（4 类，未展示） |
| 16 | YMOS | 证据新鲜度规则引擎（Profile 声明数据过期阈值，门禁统一校验） | analysis_context 有 freshness 但未关联门禁 | `services/trading/evidence_freshness.py` | 仅库实现（未接门禁） |
| 17 | YMOS | 决策窗口/冷却期（只允许收盘后形成判断，盘中修改记红旗） | 无决策窗口概念 | `services/trading/decision_window.py` | 仅库实现（未接门禁） |
| 18 | YMOS | 损失预算约束（账户级/策略级累计损失门禁） | 有单笔 maxSingleRatio，无账户级累计 | `services/trading/loss_budget.py` | 仅库实现（未接门禁） |
| 19 | YMOS | 提案生效验证（检查上一轮变更是否减少同类错误） | proposals 有 verified/rejected 但无自动回溯 | `services/trading/proposal_effectiveness.py` | 部分接入（平行实现未复用） |
| 20 | daily_stock_analysis | 决策信号生命周期（8 态 CRUD + 过期 + 对立失效 + 反馈） | signal_scorecard 仅评估，缺完整生命周期 | `services/trading/signal_lifecycle.py` | 仅库实现（未接信号链） |
| 21 | daily_stock_analysis | 多通道通知（feishu / email / ntfy / slack / pushover / gotify） | 只有飞书 / PushPlus | `services/notifications.py` | 仅库实现（dry-run，未接发送链） |

## P3 — 可选增强，暂缓

| # | 来源 | 功能 | 暂缓理由 | 状态 |
|---|---|---|---|---|
| 22 | PA_Agent | 精细化重试反馈（按错误类型注入针对性提示） | 强耦合 stage1/stage2 schema，需设计通用机制 | ⏸ |
| 23 | PA_Agent | JSON 截断诊断（花括号深度区分"思考占满"vs"没输出 JSON"） | 仅诊断价值，不改变行为 | ⏸ |
| 24 | go-stock | 财经日历展示层 | 无本地 sealed 契约，增加远端旁路 | ⏸ |
| 25 | go-stock | 新闻情感分析（金融文本情感打标） | 须走外部展示级路径 | ⏸ |
| 26 | YMOS | 诊断问答模式（独立可调用诊断 skill） | 取决于用户是否会主动调用 | ⏸ |
| 27 | YMOS | 仓位口径 sizingBasis（NAV / risk_budget / custom） | tickflow 固定 NAV 已覆盖大部分场景 | ⏸ |
| 28 | daily_stock_analysis | Vision LLM 图片股票代码提取 | 依赖外部 AI 服务 | ⏸ |
| 29 | daily_stock_analysis | 飞书云文档（Markdown 块写入飞书文档） | tickflow 有 SSE 实时推送替代 | ⏸ |
| 30 | daily_stock_analysis | 报告渲染（Jinja2 多平台渲染） | tickflow 有前端渲染替代 | ⏸ |

---

## 明确排除（红线，永不移植）

| 来源 | 排除项 | 理由 |
|---|---|---|
| PA_Agent | PyQt GUI / QThread / EventBus / Session 告警 | 技术栈红线 |
| PA_Agent | 多公网数据源 / MT5 / TradingView / factory | 数据主链路红线 |
| PA_Agent | 二元决策树 / 经验库 / 自动荐股 | 自动交易红线 |
| PA_Agent | MiMo 兼容 / WorkBuddy/QClaw/Cursor 连接器 | 外部 agent 平台 |
| PA_Agent | O(1) 增量指标（AtrState/EmaState） | 无高频 tick 消费场景 |
| Vibe-Trading | LangGraph agent / 券商实盘 / Shadow Account | 交易红线 |
| Vibe-Trading | 期权 / 加密 / 跨市场引擎 | 产品边界 |
| daily_stock_analysis | AlphaSift 多 fetcher | 多公网数据源红线 |
| daily_stock_analysis | Social Sentiment 外部 API | 外部数据红线 |
| daily_stock_analysis | Agent Framework 自动决策 | 自动交易红线 |
| YMOS | BrainStorm / Console HTML / 投研层 / Agent 角色 | tickflow 已有等价替代 |
| go-stock | AI 荐股 / 社区弹幕 | 产品边界 |

---

## 实施记录

每次完成一个候选后在此追加记录。

| # | 日期 | commit | 验证 |
|---|---|---|---|
| — | — | — | — |

| 2026-08-14 | #1–#21 | 接入审计：5 项已接入、2 项部分接入、14 项仅库实现；P3 #22–#30 继续作为显式暂缓候选，不在 PA_Agent 约定范围内 | 定向模块测试与调用链审计 |
