# PA_Agent 移植深度复评与执行记录

> 日期：2026-08-09
> 范围：`../PA_Agent/pa_agent/` 全部业务域，以及 tickflow 对应后端、前端、API 与文档契约。
> 权威账本：[PA_AGENT_PORTING_PLAN.md](./PA_AGENT_PORTING_PLAN.md)；本报告记录本次审计方法、候选排序和执行决策，不替代该账本。

## 1. 结论

本次不新增运行时代码。不是因为审计不充分，而是所有仍可识别的 PA_Agent 能力均落入下列情况之一：tickflow 已以更安全的 Web/本地数据形态覆盖；违反项目不可迁移边界；或缺少用户可见的产品契约，尚不能安全交付。

本轮选择并执行的最高价值项是**收口迁移账本并固化重启门槛**：修正计划文档中实现路径与目标结构的漂移，明确哪些候选必须继续暂缓，以及何时才允许重新立项。这个决定避免以“完整移植”为理由引入第二套数据源、交易决策语言、隐式 AI 上下文或无消费者的 API。

结论状态：

| 分类 | 条目 | 当前结论 |
|---|---|---|
| 已安全交付 | M1–M19 | 保持现状，不做重复迁移 |
| 已有能力覆盖 | M23、M24 | 保持现状 |
| 明确排除 | M20、M22、PyQt/QThread、自动交易/荐股、专用桌面连接器 | 保持排除 |
| 条件暂缓 | M21、M25、形成中多周期 K 线判定 | 条件未满足，不实现 |

## 2. 审计方法与事实基线

1. 通读 `PA_AGENT_PORTING_PLAN.md` 的候选总表、最终处置账本、P0–P5 阶段、Gate 和源证据索引；逐项比对最终处置和落点文件。
2. 审查 PA_Agent 的 `data/`、`ai/`、`orchestrator/`、`records/`、`notify/`、`util/` 与 `gui/`，区分可复用的领域无关机制和桌面/荐股强耦合实现。
3. 审查 tickflow 的 provider、复权、指标、因子/策略回测、交易门禁、计划检查、analysis artifact、监控告警、SSE、设置和 React 页面；以实际符号与调用边界而不是文档宣称为准。
4. 对候选使用四个硬门：是否有明确用户场景、是否保持本地 canonical 数据与非交易决策边界、是否存在独立消费者、是否可用确定性测试证明收益。

## 3. 对比结果

| PA_Agent 源域 | 上游机制 | tickflow 对应证据 | 结论 |
|---|---|---|---|
| `data/base.py`、`snapshot.py` | `KlineBar/KlineFrame`、EMA20/ATR14、warmup | `services/analysis_context.py` 的 `KlineAnalysisFrame`，`indicators/pipeline.py` 的 Polars 指标 | 已覆盖；不复制 dataclass 数据模型 |
| `data/refresh_loop.py`、`eastmoney_client.py` | 单飞、退避、瞬态失败 | `services/external_fallback/` 与 `fquant/sina_tencent_client.py` | M19 已交付；只允许默认关闭的展示级 fallback |
| 各公网 source、`factory.py`、MT5/TradingView | 多公网数据源与选择器 | `data_providers/registry.py` 仅保留本地 DuckDB provider | M22 明确排除；不得注册第二主数据源 |
| `data/kline_adjust.py` | 全局 qfq/hfq/none | `fquant/adj_factor.py`、canonical enriched 前复权、回测只读 enriched | M21 暂缓；上游全局可变状态不可迁移 |
| `ai/json_validator.py`、`orchestrator/validation_retry.py` | JSON 校验、重试、防篡改 | `services/ai_structured/{runtime,parser,retry,immutable,audit}.py` | M2–M4 已覆盖 |
| `orchestrator/two_stage.py`、`decision_nodes.py`、`trace_normalize.py` | Stage1/Stage2、锁定节点、决策树 | `services/trading/plan_check.py`、`AnalysisTraceNode`、`DecisionTrace.tsx` | M11–M13 已安全改写；不迁移买卖分支树 |
| `ai/decision_continuity.py`、`records/analysis_history.py` | 跨轮论点、增量 K 线、止损/限价触发 | `analysis_artifacts.py` 已有 parent 链基础；无跨日论点产品入口 | M25 暂缓 |
| `records/*`、`notify/*` | append-only 记录、失败队列、飞书/PushPlus | `analysis_artifacts.py`、`webhook_adapter.py`、`notify_adapter.py` | M15–M18 已覆盖，且密钥与通知边界更严格 |
| `gui/*` | FlowBar、决策流、调试面板、预测图 | React SSE、`AlertToast.tsx`、`DecisionTrace.tsx`、`AiExecutionMetaBadge.tsx`、Trading/Review 页面 | 当前 UI 覆盖更完整；不迁移 PyQt 实现 |

### 数据、回测与监控边界

- **数据源**：`FQuantProvider` 是唯一主 provider，本地 DuckDB 只读；腾讯外部行情仅经 `ExternalFallbackAdapter.resolve_realtime()` 进入 `api/intraday` 的展示响应，含 `source/degraded/stale_session`，不写 canonical/enriched，不进入选股、回测、监控或交易治理输入。
- **策略/回测**：策略路径已有 `StrategyBacktestService`、walk-forward、bootstrap、MC permutation 和 run card；因子路径已有 IC/IR、分层、多空、随机置换控制。因子 IC 衰减/换手/OOS 是独立研究 backlog，不是 PA_Agent 遗漏项，不能借本次迁移名义改变范围。
- **监控告警**：`MonitorRuleEngine`、`alert_store.py`、webhook/PushPlus 适配器、`useQuoteStream.ts` 与 `AlertToast.tsx` 已覆盖 PA_Agent 的本地 toast/通知语义，且无自动执行端点。
- **API/UI**：现有 `/api/trading/plan-checks` 以领域接口暴露 artifact、导出和 replay plan。当前没有第二种持久化 AI artifact producer，故不增设泛化 artifact API 来重复计划检查接口。

## 4. 剩余候选排序与决策

| 优先级 | 候选 | 价值 | 风险/阻断 | 本次决定 |
|---:|---|---|---|---|
| 1（未来重启） | M25 跨日连续性与增量分析 | 高：可解释同一论点的变化 | 缺用户入口、parent 选择、失效语义、成本展示与 stale replay 契约 | 暂缓；技术依赖 M16 已满足，但产品门未满足 |
| 2（未来重启） | M21 查询级 qfq/hfq/none | 中：图表/分析层可选口径 | 无用户场景；会扩大缓存键、跨市场口径和回测一致性面 | 暂缓 |
| 3（条件候选） | `bar_close_wait.py` 多周期 forming-bar 判定 | 中：实时分钟 K 线分析时可避免使用未收盘 bar | tickflow 当前不消费实时分钟 K 线流，daily canonical 数据本身已收盘 | 暂缓，不能预置无消费者的抽象 |
| 4 | 泛化 analysis artifact API | 低中 | 当前只有计划检查产生日志，泛化端点会复制既有领域 API | 不实现 |
| 5 | PA 因子/策略执行纪律 | 低 | tickflow 回测/门禁已有更强实现；上游绑定荐股/执行 | 不迁移 |
| 6 | 多 Host/TLS 指纹、多公网适配器、桌面 GUI | 无法抵消风险 | 违反服务条款、数据主链路或技术栈边界 | 明确排除 |

## 5. 已执行的高价值动作

1. 对 `PA_AGENT_PORTING_PLAN.md` 作追溯性修正：标明候选总表的“建议”是历史评估，当前应以最终处置账本为准；补全 P5 的真实文件路径；说明结构化校验职责已并入 `runtime.py` 和 `immutable.py`，没有名为 `validator.py` 的遗失实现；明确 P5 的 M21/M25 评估任务已完成且结论为暂缓。
2. 在计划中新增本次深度复评记录与重启门，防止后续维护者把暂缓项误解为遗漏实现。
3. 未改变任何 runtime、数据模型、接口或功能开关，因此不需要伪造“新功能测试通过”的结论；本次验证重点是文档指向、源码落点和边界事实。

## 6. M21/M25 重新立项的必要条件

### M21 查询级复权

必须同时满足：

1. 有具体用户任务，且明确涉及标的、页面/API 与 qfq/hfq/none 预期；
2. 明确查询层调整只服务展示/AI 上下文，canonical enriched 与回测输入不变；
3. 为除权日前后 fixture 建立 qfq/hfq/none 对拍，并验证缓存键、跨市场与 API provenance；
4. 所有调用方显式获得 `adjustment` 标签，不能隐式沿用默认。

### M25 连续性分析

必须同时满足：

1. 有用户可见的“同一标的/论点跨日追踪”入口，而非后台隐式续写；
2. 明确选择哪个成功 artifact 为 parent、何时建立新链、何时失效；
3. 失效、数据过期或模型/profile 改变时强制回到全量分析；
4. 每次运行产生新的 append-only artifact，保留 parent 关系并展示实际成本；
5. 分析只解释已有事实，不能生成订单、方向、建议价格或执行动作，也不能写 trade event 或进入 screener/backtest/monitor。

## 7. 验证记录

- 已直接核对 `backend/app/services/analysis_context.py`：默认排除显式 `closed=false` 的 bar；当前 stock analysis 和 plan check 都从 canonical enriched 日 K 构建 `1d` frame。
- 已直接核对 `backend/app/markets.py`：A 股/HK 的交易时段集中定义，PA_Agent 的形成中多周期逻辑没有当前消费者。
- 已直接核对 `backend/app/services/stock_analyzer.py` 与 `services/trading/plan_check.py`：两者共享只读 canonical 日 K 分析帧，不导入 PA_Agent 数据源或交易执行语义。
- 已直接核对计划账本与落点目录：M1–M19 均有现有模块支撑；M20/M22 排除，M21/M25 暂缓，M23/M24 为既有覆盖。

## 8. 后续维护

本报告不创建新的迁移待办。未来只在第 6 节的全部前置条件被满足后，才对 M21 或 M25 单独立项、先写契约/fixture，再做实现。其余 PA_Agent 源码仍作为机制参考，不作为待复制的功能清单。
