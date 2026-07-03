# Vibe-Trading 可迁移候选调研

- 调研日期: 2026-07-03
- Vibe 源仓库: `/Users/wf2311/Projects/wf2311/fm/Vibe-Trading`
- 当前项目: `/Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel`
- 范围: 只做本地代码与文档调研评估, 不写业务代码。

## 结论摘要

Vibe-Trading 的核心优势不是单个指标, 而是“研究工作流外壳”: 交易日志复盘、Shadow Account、研究假设/证据链、定时研究、Alpha Zoo 横评、文档/网页摄取、MCP tool 暴露和技能方法论库。tickflow-stock-panel 已经有更强的 A 股工作台底座: 本地数据源、选股/监控/回测、复盘报告、扩展数据和实时深度能力。因此迁移应避免照搬 Vibe 的 LangGraph/swarm/多市场 loader 框架, 只挑能接入现有 UI 和 service 的薄模块。

最高优先级候选:

1. **Trade Journal + Shadow Account**: 高价值/中成本。panel 的“交易”页还是占位, 这是最契合个人 A 股工作台的新增主功能。
2. **研究假设 registry + 回测证据链**: 高价值/低中成本。可直接增强策略池、回测页、AI 复盘的可追踪性。
3. **A 股参考数据工具剩余 7 件套**: 高价值/低成本。继龙虎榜后继续接入北向、两融、大宗、股东户数、解禁、研报/EPS、新闻到 ext_data。
4. **Alpha Zoo 元数据/bench/compare 框架**: 高价值/中成本。当前只接了 `alpha101_001`; 下一步不应一次搬 456 个, 而是先搬 registry/manifest/compare/strict bench 形态。
5. **Universal document/web reader**: 中高价值/低成本。给 AI 四维分析、复盘、财报解读提供用户上传材料入口。

明确不建议近期迁移:

- LangGraph/LangChain agent 框架、swarm runtime、IM 双向 channel runtime。
- 多市场 backtest engines/loaders 整套框架。
- 实盘券商连接器和自主下单 runtime。
- 期权链/期权定价、crypto/DeFi 专用工具, 除非产品定位扩展。

## 2026-07-03 Grilling 修订记要（覆盖上文冲突处，以本节为准）

本节是对下文的一轮 grill + domain-modeling 复审结论。**凡与下文冲突，以本节为准**；术语精确定义见 `CONTEXT.md`「交易复盘领域」。

**R1. 落地不是线性序列，是两条并行轨（覆盖「推荐落地路线」与「最小下一步」的自相矛盾）**
- **Track A（低成本增量）**：C3 剩余 A 股参考数据源，随做随用，不阻塞任何东西。
- **Track B（旗舰功能）**：Trade Journal → （延后）Shadow Account。
- 原文「最小下一步 = C1 Shadow Account」是错的表述：C1 的 MVP 是 Trade Journal，不是 Shadow Account。

**R2. C1 拆成两个术语两件事（覆盖 C1 标题「Trade Journal + Shadow Account」的打包）**
- **Trade Journal（Track B 第一里程碑，成本：中）**：真实成交流水 → FIFO roundtrip → PnL/行为诊断。是纯事实性诊断。
- **Shadow Account（显式延后、独立立项，成本：高）**：抽 if-then 规则 → 回放成假设组合 → delta-PnL 反事实。原文把它藏在 C1 的 Phase 2/3，低估了成本（Vibe 源实测 shadow_account/ 全套 ≈ 3654 行）。是否启动取决于 Trade Journal 诊断是否够好。

**R3. 行为诊断第一阶段用纯统计，不经 LLM（原文完全漏掉的隐私风险）**
- 处置效应/过度交易/追涨/锚定四项均可由 roundtrip 台账闭式计算，不需要 LLM。真实券商流水是用户最敏感数据，MVP 阶段"送外部 LLM"的路径根本不建路。Hybrid（仅送聚合数字写叙事）作为后续 opt-in。

**R4. 解析用「通用列映射 + 券商预设」，不硬编码 per-broker parser**
- 真实样本 `银河.xlsx`（同花顺投资账本导出）是 **3-sheet xlsx**（持仓数据/已清仓/交易记录），非单张 CSV。
- **事实源 = 「交易记录」sheet 的原始逐笔成交，自己跑 FIFO**；「已清仓」sheet（同花顺预配对的 roundtrip）**只作对拍 oracle**（验证我方 FIFO 正确性，套路同 raw-reconstruct 用 fstore oracle），不作数据源——否则重蹈"依赖券商专有格式"脆弱性。
- 解析须处理：`交易类别 ∈ {买入,卖出,…}` 过滤非交易行（银行转证券）；`发生金额` 买入负/卖出正、`费用` 单列；代码 A 股/港股混合（5 位前导零=HK，复用 P6 判定）。

**R5. Trade Journal 独立新模块，不塞进 backtest 引擎**
- FIFO 对账 ≠ 回测模拟，是两种算法。Trade Journal 落 `backend/app/services/trade_journal/`（parser/mapping/fifo/diagnose/report）。原文「复用 `backtest/engine.py`」**只适用于延后的 Shadow Account**（规则回放才是真回测）。
- 持久化：roundtrip 台账 → `data/user_data/trade_journal/`（对齐 `strategy_cache.py`）；诊断报告 → 复用现有 report store；**上传原始 xlsx 解析完即弃，不长期落盘**（隐私红线）。

**R6. 基准对比：账户-区间超额为主，逐笔超额为辅**
- 头条 = 整个台账窗口内已实现收益 vs 沪深300（统计稳健）；逐笔"跑赢大盘"作附表并标注"短周期超额噪声大"。
- **港股回合基准存疑**：P6 只加了港股个股日 K，恒生指数数据未确认；若无，港股回合先只出绝对盈亏、不硬凑错基准。

**R7. C3 剔除「北向资金」（High 级事实错误订正）**
- 北向（沪深股通）日度净流入数据自 **2024 年 8 月起已停止披露**。Vibe `northbound_tool.py` 查的东方财富 `kamt`/`kamt.kline` 接口只剩冻结历史或空实时。原文 C3 第 4 项「北向 `ext_northbound_em`：复盘材料」是接一个已死的源，应剔除或降级为"仅历史存量、不建议接入"。

**R8. 遗漏候选补录（原文漏列的低耦合高价值项）**
- **C13 技术形态识别**（`agent/src/tools/pattern_tool.py`）：纯 pandas/numpy 峰谷/形态检测，零 LangGraph 耦合，可直接接回测/个股分析页。价值中、成本低。
- C3 限流可直接抄 Vibe `agent/backtest/loaders/_http.py` 的 `HostThrottle`（per-host 最小间隔+抖动+共享 session），别空写"需退避"。

**R9. 数字订正（不影响判断，供引用准确）**
- 「54 个工具」→ 实为 46 个 `*_tool.py` 文件 / 72 个 `BaseTool` 子类。
- 「18 loader」→ 实为 24 个 loader 文件、约 21 个源品牌。
- C6 的 SSRF 拦截在 `web_reader_tool.py` 本体（scheme+私网 IP），`security/scanner.py` 其实是 prompt-injection 扫描器，两者别混。C6 的 OCR 依赖是 `rapidocr_onnxruntime`（ONNX，较重），非 pytesseract。
- C4 的"AST 静态门禁"实为"不执行模块地提取 `__alpha_meta__` 元数据"，不是对 `compute()` 体内 import/副作用的门禁——panel 扩因子仍需自写纯函数校验。
- panel 回测基线被原文低估：`factor.py`/`engine.py` 已有 IC/IR、多空组合、Calmar，非"仅基础 stats"。C5 成本应按"在较完整统计层上加稳健性检验"算，不是从零建。

## 当前项目基线

tickflow-stock-panel 已有能力:

- 数据源抽象: `backend/app/data_providers/`, `tickflow` / `fquant` / `fquant_local`。
- A 股工作台: 自选、选股、监控、连板梯队、深度封单、指数/ETF、扩展数据。
- 回测: `backend/app/backtest/engine.py`, `factor.py`, `optimizers.py`; 纯 Polars/NumPy。
- AI: 个股分析、财务分析、大盘复盘、轻量 agent API。
- 扩展数据: `backend/app/services/ext_data.py`, `ext_presets.py`, `ext_pull.py`。
- 告警: 飞书/钉钉/企微/MeoW webhook。
- 交易页: `frontend/src/pages/Trading.tsx` 仍是“信号到 QMT/掘金/Ptrade”的占位规划。

近期已从 Vibe 方向落地的部分:

- P5 多通道告警。
- P6 港股磁盘日 K 路径兼容。
- P3 组合优化器。
- P7 轻量 agent tools API。
- P2 首个 Alpha101 因子。
- P1 首个 ext preset: 东方财富龙虎榜。

## 逐模块对比

| Vibe 模块 | Vibe 能力 | panel 当前状态 | 缺口 | 迁移判断 |
|---|---|---|---|---|
| `agent/src/tools/*_tool.py` | 54 个 MCP/agent 工具, 覆盖行情、A 股参考数据、文档、网页、交易日志、Shadow Account、研究目标 | panel 只有轻量 `agent_tools.py` 两个工具; 业务能力主要以 REST/UI 存在 | 缺 tool schema 层和多个数据工具 | 选“业务数据工具”和“薄 tool schema”, 不搬 shell/file edit/swarm |
| `agent/src/tools/{fund_flow,northbound,margin_trading,block_trades,shareholder_count,lockup_expiry,research_reports,stock_news}_tool.py` | 东方财富/数据中心解析, 免 key A 股参考数据 | panel ext_data 已能存 snapshot/timeseries; 仅概念/行业/龙虎榜内置 | P1 八件套只完成龙虎榜 | 高优先级逐源接 `ext_presets` |
| `agent/src/shadow_account/` + `trade_journal_tool.py` | 解析券商流水, 行为诊断, 抽规则, shadow 回测, HTML/PDF 报告, 今日信号扫描 | panel 无交易日志/行为复盘; Trading 页只是占位 | 缺“我的真实交易 vs 策略/影子规则”闭环 | 高优先级独立功能 |
| `agent/src/hypotheses/`, `goal/`, `autopilot_tool.py` | 研究假设 registry、状态流转、证据、回测 run card 回写 | panel 有策略配置/回测结果, 但没有假设生命周期和证据链 | 策略研究不可追踪, AI 输出难审计 | 高价值, 做轻量本地 JSON/Parquet 版 |
| `agent/src/scheduled_research/` | cron/interval 定时研究执行器 | panel 有定时复盘、盘后 pipeline、scheduler | 定时任务只围绕复盘/数据同步, 没有“任意研究任务” | 中高价值, 在 Review/Strategy 后补 |
| `agent/src/factors/` | 456 因子 registry, metadata, AST 静态门禁, bench, compare, strict random-control | panel 仅 `alpha101_001`, 因子回测 UI 已有 | 缺 registry/manifest/批量横评/严格验证 | 高价值中成本, 分两步迁移框架再迁移因子 |
| `agent/backtest/validation.py`, `run_card.py` | Monte Carlo、Bootstrap Sharpe CI、Walk-forward、run_card 可复现摘要 | panel 回测有基础 stats 和曲线, 没有显著性/稳健性验证和 run card | 回测结果容易过拟合, 缺可复现证据 | 中高价值低中成本 |
| `agent/src/tools/doc_reader_tool.py`, `web_reader_tool.py` | PDF/DOCX/XLSX/PPTX/图片 OCR/网页 Markdown 摄取 | panel AI 分析主要吃行情/财务/复盘上下文, 无用户材料摄取 | 不能把研报、公告、截图、网页喂入分析 | 中高价值低成本 |
| `agent/src/skills/` | 79 个金融方法论 skill, 多数是 Markdown 指南+少量 example_signal_engine | panel 有策略指南和 AI prompt, 无可浏览/加载的技能库 | 缺系统化知识库, 但不是计算代码 | 中价值低成本, 先搬 A 股/风控/复盘相关 Markdown |
| `agent/src/live/mandate`, `halt.py`, `trading/` | 实盘 mandate gate、kill switch、只读/实盘连接器 profile | panel Trading 页未实现, 监控可产生信号 | 如果未来接 QMT/掘金, 缺安全合约和熔断 | 只借鉴安全模式, 暂不搬连接器 |
| `agent/src/channels`, `channelsui` | 16 个双向 IM channel runtime, pairing, status/start/stop | panel 已有单向 webhook 告警 | 双向对话 panel 需求未明确 | 近期不搬; 单向 webhook 已够 |
| `agent/backtest/engines`, `loaders` | 7 引擎 + 18 数据源, 多市场自动 fallback | panel 专注 A 股本地数据源, provider 抽象已收口 | 多市场扩张会打散当前边界 | 不搬框架; 只参考 HK/本地 loader 细节 |
| `agent/src/tools/options_*`, crypto/macro/SEC/yfinance 等 | 期权、加密、FRED、SEC、美股/HK profile | panel 定位 A 股为主 | 产品定位不匹配 | 暂不迁移 |

## 候选清单与优先级

### C1. Trade Journal Analyzer + Shadow Account

- Vibe 来源:
  - `agent/src/tools/trade_journal_tool.py`
  - `agent/src/tools/trade_journal_parsers.py`
  - `agent/src/shadow_account/models.py`
  - `agent/src/shadow_account/{extractor,backtester,reporter,scanner,codegen}.py`
- panel 对应入口:
  - `frontend/src/pages/Trading.tsx` 当前只是占位。
  - `backend/app/backtest/engine.py` 可作为 shadow 回测执行底座。
  - `backend/app/services/ai_reports.py` / stock/review report 模式可复用报告保存。
- Vibe 有但 panel 没有:
  - 券商导出解析: 同花顺/东财/富途/generic CSV。
  - FIFO 配对成交, 计算 roundtrip PnL、持仓天数、胜率、盈亏比、回撤。
  - 行为偏差诊断: 处置效应、过度交易、追涨、锚定。
  - 从盈利 roundtrip 抽取 3-5 条 if-then 个人规则。
  - 用影子规则回测并与真实交易做 delta-PnL 归因。
  - HTML/PDF 个人复盘报告和今日 shadow 信号扫描。
- 价值/成本:
  - 价值: 高。直接补齐“个人工作台”最缺的交易复盘功能。
  - 成本: 中。解析/诊断可直搬思路; backtest/report 需 panel 化。
  - 风险: 中。真实交易流水格式脏, 需要导入预览和字段映射。
- 推荐迁移方式:
  - 第一阶段只做“上传交易流水 → 行为诊断报告”, 不做自动抽规则。
  - 第二阶段做 ShadowProfile 数据模型和规则提取。
  - 第三阶段接 panel 回测引擎和今日信号扫描。
  - 不搬 Vibe 的 PDF 字体下载和多市场 shadow 回测, 先 A 股。

### C2. 研究假设 Registry + 证据链 + Run Card

- Vibe 来源:
  - `agent/src/hypotheses/registry.py`
  - `agent/src/tools/hypothesis_tool.py`
  - `agent/src/goal/models.py`
  - `agent/src/tools/autopilot_tool.py`
  - `agent/backtest/run_card.py`
- panel 对应入口:
  - 策略池、策略回测、因子回测、AI 复盘。
  - `backend/app/api/backtest.py`, `backend/app/backtest/engine.py`, `frontend/src/pages/backtest/*`。
- Vibe 有但 panel 没有:
  - 假设生命周期: exploring/testing/validated/rejected/monitoring。
  - 假设关联 universe、signal_definition、data_sources、skills、run_cards。
  - 回测 artifact hash、config hash、strategy hash、warnings。
  - 证据和完成标准的结构化 ledger。
- 价值/成本:
  - 价值: 高。能把“AI 生成策略/人工策略/回测结果”变成可追踪研究资产。
  - 成本: 低中。先做本地 JSON store + UI 列表即可。
  - 风险: 低。与交易无关, 不碰数据源。
- 推荐迁移方式:
  - 在 panel 新增“研究假设”轻量模型: title/thesis/status/universe/signal/run_ids。
  - 每次回测保存 run_card.json/md。
  - 策略详情页显示关联假设和证据。

### C3. A 股参考数据工具剩余源接入 ext_presets

- Vibe 来源:
  - `fund_flow_tool.py`, `northbound_tool.py`, `margin_trading_tool.py`
  - `block_trades_tool.py`, `shareholder_count_tool.py`, `lockup_expiry_tool.py`
  - `research_reports_tool.py`, `stock_news_tool.py`, `sector_tool.py`
- panel 对应入口:
  - `backend/app/services/ext_presets.py`
  - `backend/app/services/ext_data.py`
  - `backend/app/api/ext_data.py`
  - 个股分析、复盘、连板梯队、扩展数据页。
- 已有:
  - 概念、行业、龙虎榜。
  - moneyflow 本地/上游已有 provider 能力, 但 ext_data 维度未系统化。
- Vibe 有但 panel 没有:
  - 北向资金、融资融券、大宗交易、股东户数、限售解禁、研报/EPS 一致预期、新闻。
  - sector 工具的行业/概念成员与表现查询。
- 价值/成本:
  - 价值: 高。
  - 成本: 低。多数是 HTTP+解析+schema, 可接现有 ext_presets。
  - 风险: 中。东方财富接口字段和限流会变; 需要 `trust_env=False`、host allowlist、退避。
- 推荐迁移顺序:
  1. 解禁 `ext_lockup_em`: 对连板/短线风险最有用。
  2. 股东户数 `ext_holder_count_em`: 个股筹码变化。
  3. 两融 `ext_margin_em`: 市场情绪和杠杆资金。
  4. ~~北向 `ext_northbound_em`~~: **剔除**——北向日度净流入 2024-08 起停止披露（见 R7），源已死。
  5. 大宗交易 `ext_block_trade_em`: 个股异动解释。
  6. 研报/EPS `ext_research_eps_em`: 四维分析。
  7. 新闻 `ext_news_em`: 仅做标题素材, 不做全文抓取。

### C4. Alpha Zoo Registry / Manifest / Compare / Strict Bench

- Vibe 来源:
  - `agent/src/factors/registry.py`
  - `agent/src/factors/base.py`
  - `agent/src/factors/bench_runner.py`
  - `agent/src/factors/bench_runner_strict.py`
  - `agent/src/tools/alpha_bench_tool.py`
  - `agent/src/tools/alpha_compare_tool.py`
- panel 对应入口:
  - `backend/app/backtest/factor_zoo.py`
  - `backend/app/backtest/factor.py`
  - `frontend/src/pages/backtest/FactorBacktest.tsx`
- 已有:
  - 一个 Polars 因子 `alpha101_001`。
  - 因子回测 IC/IR 和分层 UI。
- Vibe 有但 panel 没有:
  - alpha metadata: theme、formula_latex、columns_required、warmup、universe。
  - registry list/get/health/export_manifest。
  - 批量 bench、compare、strict random-control、OOS split。
  - 大规模 zoo 的纯函数/AST 门禁。
- 价值/成本:
  - 价值: 高。
  - 成本: 中。registry/manifest 低成本; 因子逐个 Polars 化成本高。
  - 风险: 中高。pandas→Polars 翻译容易错, 必须保留 golden 对拍。
- 推荐迁移方式:
  - 先搬“元数据和 registry 形态”, 不急着搬 456 因子。
  - 先扩 10 个 Alpha101, 每个带 pandas golden fixture。
  - 再做 `alpha_compare` 和 strict bench, 接因子回测页。

### C5. 回测稳健性验证

- Vibe 来源:
  - `agent/backtest/validation.py`
  - `agent/backtest/metrics.py`
  - `agent/backtest/run_card.py`
- panel 对应入口:
  - `backend/app/backtest/engine.py`
  - `backend/app/api/backtest.py`
  - `frontend/src/pages/backtest/*`
- Vibe 有但 panel 没有:
  - Monte Carlo permutation p-value。
  - Bootstrap Sharpe CI。
  - Walk-forward 分窗稳定性。
  - per-symbol/per-exit-reason 统计。
- 价值/成本:
  - 价值: 中高。减少策略过拟合误判。
  - 成本: 低中。NumPy/Polars 可直接实现。
  - 风险: 低。纯后处理, 不影响撮合。
- 推荐迁移方式:
  - 在回测请求增加 `validation: true` 或 UI 开关。
  - 默认只跑轻量 walk-forward; Monte Carlo/Bootstrap 用户手动开启。

### C6. Universal Document Reader + Web Reader

- Vibe 来源:
  - `agent/src/tools/doc_reader_tool.py`
  - `agent/src/tools/web_reader_tool.py`
  - `agent/src/security/scanner.py`
- panel 对应入口:
  - `backend/app/services/stock_analyzer.py`
  - `backend/app/services/financial_analyzer.py`
  - `backend/app/services/market_recap.py`
  - 前端 AI 分析对话框。
- Vibe 有但 panel 没有:
  - PDF/DOCX/XLSX/PPTX/image OCR 统一文本提取。
  - 网页转 Markdown。
  - 安全扫描/截断 envelope。
- 价值/成本:
  - 价值: 中高。用户可以把公告、研报、截图、网页喂给 AI。
  - 成本: 低到中。文本/Excel 可低成本; OCR/PDF 依赖要谨慎。
  - 风险: 中。新增依赖和 SSRF/本地文件边界。
- 推荐迁移方式:
  - 第一阶段只支持 `.txt/.md/.csv/.xlsx/.pdf` 文本提取, 不做 OCR。
  - 网页读取只允许 http/https 公网, 明确 SSRF 拦截。
  - 生成“附件摘要”后并入 AI prompt, 不把原文长期存储。

### C7. Finance Skills 方法论库

- Vibe 来源:
  - `agent/src/skills/*/SKILL.md`
  - A 股相关: `eastmoney`, `tushare`, `mootdx`, `ashare-pre-st-filter`, `sector-rotation`, `trade-journal`, `shadow-account`, `alpha-zoo`, `factor-research`, `risk-analysis`, `backtest-diagnose`, `technical-basic`, `candlestick`, `chanlun`, `multi-factor`, `market-microstructure`。
- panel 对应入口:
  - `docs/strategy-guide.md`
  - `backend/app/strategy/prompt_builder.py`
  - AI 策略生成、复盘、个股分析。
- Vibe 有但 panel 没有:
  - 可枚举/可加载的金融方法论文档。
  - 每类分析的结构化 prompt/模板。
- 价值/成本:
  - 价值: 中。
  - 成本: 低。多数是 Markdown。
  - 风险: 低中。要筛掉美股/crypto/期权等无关内容, 避免污染 A 股产品定位。
- 推荐迁移方式:
  - 建 `docs/skills/` 或 `backend/app/services/knowledge/` 静态知识库。
  - UI 先不做复杂 skill manager, 只在 AI prompt 中按场景加载对应 Markdown。

### C8. MCP Server / Tool Schema 暴露

- Vibe 来源:
  - `agent/SKILL.md`
  - `agent/src/tools/__init__.py`
  - `agent/src/tools/mcp.py`
- panel 对应入口:
  - `backend/app/services/agent_tools.py`
  - `backend/app/api/agent.py`
- 已有:
  - `/api/agent/tools` 和 `/api/agent/chat`。
- Vibe 有但 panel 没有:
  - 独立 MCP server 命令。
  - 完整 tool registry、schema 规范化、外部 MCP client 适配。
- 价值/成本:
  - 价值: 中。对 Claude/Cursor 接 panel 很有用。
  - 成本: 中。要做进程入口、鉴权、工具 allowlist。
  - 风险: 中。工具暴露边界和本地数据访问要收紧。
- 推荐迁移方式:
  - 不搬 Vibe MCP client 复杂逻辑。
  - 只做 panel 自己的 MCP server: tools 复用 `agent_tools.TOOLS`。
  - 首批工具: capabilities、list_strategies、run_screener、get_kline、get_overview、run_backtest、list_ext_data。

### C9. 定时研究 / 自动复盘任务

- Vibe 来源:
  - `agent/src/scheduled_research/models.py`
  - `agent/src/scheduled_research/executor.py`
- panel 对应入口:
  - `backend/app/jobs/daily_pipeline.py`
  - `backend/app/services/preferences.py`
  - `frontend/src/pages/Review.tsx`
- Vibe 有但 panel 没有:
  - 用户自定义 prompt + interval/cron 的研究任务。
  - job lifecycle: pending/running/completed/failed/cancelled。
- 价值/成本:
  - 价值: 中。
  - 成本: 低中。APScheduler 已在项目内。
  - 风险: 中。AI 调用成本和重复任务需要限额。
- 推荐迁移方式:
  - 先扩展“定时复盘”为“定时研究模板”: 大盘复盘/自选复盘/策略池周报。
  - 不做任意 prompt 的自由调度, 先模板化。

### C10. Live Mandate Gate / Kill Switch 设计

- Vibe 来源:
  - `agent/src/live/mandate/model.py`
  - `agent/src/live/halt.py`
  - `agent/src/live/order_guard.py`
  - `agent/src/trading/profiles.py`
- panel 对应入口:
  - `frontend/src/pages/Trading.tsx`
  - 未来 QMT/掘金/Ptrade 桥接。
- Vibe 有但 panel 没有:
  - 用户授权 mandate: 单笔、总敞口、杠杆、每日次数、品种白名单。
  - 文件哨兵 kill switch。
  - 审计记录和 fail-closed 下单守卫。
- 价值/成本:
  - 价值: 高, 但只在真正做交易执行时成立。
  - 成本: 中。
  - 风险: 高。涉及实盘和用户资金。
- 推荐迁移方式:
  - 近期只写 ADR/设计, 不实现交易执行。
  - 若做 QMT 文件信号桥接, 先实现 kill switch + 只读信号导出, 不直连下单。

### C11. Strategy Export: Pine/TDX/MT5/vnpy

- Vibe 来源:
  - README 提到 `/pine` 导出 TradingView Pine Script、TDX、MetaTrader 5。
  - `agent/src/skills/pine-script`, `vnpy-export`, `strategy-generate`。
- panel 对应入口:
  - 策略构建器、AI 策略生成、自定义信号。
- Vibe 有但 panel 没有:
  - 将策略导出到外部平台的模板。
- 价值/成本:
  - 价值: 中。对用户把 panel 信号拿到外部软件有帮助。
  - 成本: 中。需要限定 DSL 子集。
  - 风险: 中。导出语义容易与 panel 指标口径漂移。
- 推荐迁移方式:
  - 只做 TDX/同花顺公式导出, 不做 Pine/MT5。
  - 仅支持无状态日线信号和已有指标列。

### C12. Symbol Search / Market Screener 工具

- Vibe 来源:
  - `agent/src/tools/symbol_search_tool.py`
  - `agent/src/tools/market_screener_tool.py`
- panel 对应入口:
  - `backend/app/api/kline.py` search, `backend/app/services/screener.py`。
- Vibe 有但 panel 没有:
  - 跨市场 symbol search。
  - 外部条件选股 API。
- 价值/成本:
  - 价值: 低中。panel 已有本地 instruments 和策略选股。
  - 成本: 低。
  - 风险: 中。外部 screener 会和本地数据口径不一致。
- 推荐迁移方式:
  - 只借鉴 symbol search 的 Eastmoney suggest 做补全增强。
  - 不迁移外部 screener, panel 继续以本地策略引擎为准。

## 推荐落地路线

### Phase 1: 低成本补强 ext_data 和研究证据链

1. P1 剩余 A 股参考数据源按 C3 顺序接入。
2. 增加 run_card: 每次策略/因子回测保存 config hash、数据范围、指标和 warnings。
3. 研究假设 registry MVP: 新增、搜索、关联回测。

### Phase 2: Shadow Account MVP

1. 交易流水上传和字段映射。
2. FIFO roundtrip + 行为诊断。
3. 报告保存到 panel 现有 report store 风格。
4. 后续再接规则提取和 shadow 回测。

### Phase 3: Alpha/回测严肃化

1. 因子 registry + metadata manifest。
2. Alpha101 扩到 10 个, 每个 golden 对拍。
3. `alpha_compare` + strict random-control bench。
4. 回测 validation: walk-forward 默认, Monte Carlo/Bootstrap 可选。

### Phase 4: AI 输入增强和外部集成

1. 文档/网页 reader 给 AI 分析加附件上下文。
2. MCP server 暴露 panel tools。
3. 技能库按 A 股场景接入 prompt。

### Phase 5: 交易桥接前置安全

1. 先落 mandate/kill switch 设计。
2. 只做信号文件导出到 QMT/掘金脚本。
3. 直连下单另立项, 不与研究迁移混做。

## 不建议迁移清单

| 能力 | 不迁移理由 |
|---|---|
| LangGraph/LangChain/swarm runtime | panel 已有明确 UI 工作流; 引入会增加大量会话状态复杂度 |
| 双向 IM channel runtime | 当前需求是告警推送, 已用 webhook 满足 |
| 多市场 loader/engine 框架 | panel 数据边界刚收口到 provider/fquant_local, 不应再铺 18 源 fallback |
| 实盘券商连接器 | 高风险, 与当前“选股+监控+回测”阶段不匹配 |
| options/crypto/DeFi/FRED/SEC 工具 | 价值偏离 A 股主线 |
| shell/file edit/background tools | 对 panel Web 服务不安全, 也不是产品能力 |

## 验证方法

本调研基于以下本地证据:

- Vibe README/SKILL: `README_zh.md`, `agent/SKILL.md`
- Vibe tools: `agent/src/tools/*.py`
- Vibe Shadow Account: `agent/src/shadow_account/*.py`
- Vibe research workflow: `agent/src/hypotheses`, `agent/src/goal`, `agent/src/scheduled_research`
- Vibe factors/backtest: `agent/src/factors`, `agent/backtest`
- Vibe live safety: `agent/src/live`, `agent/src/trading`
- panel backend: `backend/app/api`, `backend/app/services`, `backend/app/backtest`, `backend/app/strategy`
- panel frontend: `frontend/src/pages`, `frontend/src/components`

## 最小下一步

若只开一个后续开发分支, 建议选 **C1 Shadow Account MVP**。它复用现有回测/报告/UI 能力, 避免引入 Vibe agent 框架, 同时补齐 panel 当前 Trading 页的实际产品价值。
