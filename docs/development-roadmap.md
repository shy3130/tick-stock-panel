# tickflow-stock-panel 后续开发路线图

- **日期**：2026-07-03
- **依据**：综合 `docs/vibe-trading-migration-candidates.md`（Vibe 迁移候选，含 R1-R9 grilling 修订）、`docs/fquant-local-tickflow-removal-audit.md`（去 TickFlow 审计）、本会话的 Trade Journal MVP / ETF 接入实现、以及 Grilling 三题裁决。
- **范围**：梳理全部待开发/待补全功能，按优先级与依赖排序，标注工作量与关键风险，给出阶段划分。
- **工作量口径**：S=半天内、M=1-2 天、L=3-5 天、XL=1 周以上（含测试+对拍+验收）。

---

## 一、当前基线（已完成，部分待提交）

划清"已做"边界，避免把已实现的列成待办。

| 能力 | 状态 | 说明 |
|---|---|---|
| **fquant_local 本地数据源模式** | ✅ 已提交 | 磁盘直读 + raw 前复权污染修复 + raw mirror 写门控 + 本地直算流水线；核心行情面已不走 TickFlow SDK |
| **Trade Journal MVP**（Vibe C1 里程碑1） | ✅ 已提交(a4aa8b7)+ 会话内精修 | xlsx/CSV 上传→列映射→FIFO position-cycle 配对（同日买回规则，446/446 对拍）→四项行为诊断（纯统计，零 LLM）→total_pnl 双口径→账户/逐笔基准超额→追涨（含 ETF） |
| **基准超额 + 宽基指数回填** | ✅ 未提交 | 000300/000905/399006/000688 从 TDX 磁盘回填 2015→今 |
| **ETF 日线接入**（fstore） | ✅ 未提交 | provider 按 asset_type 路由 t_20_day_klines + 全量 1930 只 ETF 回填 + 符号辨识（代码区间判 ETF，可转债边界已修） |
| **P1 龙虎榜 ext preset** | ✅ 未提交 | 东方财富龙虎榜接 ext_presets |
| **P2 Alpha101_001 因子** | ✅ 未提交 | 单因子 + pandas 黄金对拍 |
| **P3 组合优化器** | ✅ 未提交 | risk_parity(CCD)/mean_variance/max_diversification/equal_vol |
| **P5 告警多通道** | ✅ 未提交 | 飞书/钉钉/企微/MeoW，含 SSRF allowlist |
| **P6 港股日 K** | ✅ 未提交 | 磁盘 hk 分桶接入 |
| **P7 agent tools 骨架** | ✅ 未提交 | /api/agent tools + chat，2 个工具 |

> **Phase 0 前置**：上述"未提交"项需先分块 commit（详见 §四 Phase 0）。

---

## 二、待开发功能全量清单

按四条轨归类。每项标 **工作量 / 关键风险 / 依赖**。

### Track A — 去 TickFlow（依据审计文档 + Q1 裁决：价值优先序）

| ID | 功能 | 工作量 | 关键风险 | 依赖 |
|---|---|---|---|---|
| **A1** | 分钟K month 扩展去 `tier_label()==expert` 门控 → 换 provider capability / 本地配置 | S | **裁决硬约束**：不得裸删，原"month 成本高"是成本考量，裸删=无条件放行（另一种 bug）；需替换成能表达"该 provider 能否负担月度分钟K"的门控 | 无（独立 quick-win） |
| **A2** | capability 语义中性化：`Cap/CapabilitySet/CapabilityDenied` 迁到中性模块；文案从"套餐升级"改"数据源能力" | L | UI/API 多处依赖旧 Cap 语义（/api/capabilities、settings、health、main.py 异常处理）；改动面广易漏 | 无（与 A3 并行） |
| **A3** | `app.tickflow.repository` rename 到中性包名（保留一版兼容导入） | M | **8 处导入方**（main / backtest.engine / daily_pipeline / screener / index_sync / extend_history / services.backtest / kline_sync）——审计原写 5 处，务必按 8 处改，别漏 | 无（纯机械，可并行） |
| **A4** | settings/health/capabilities 去 TickFlow 展示：key/tier/endpoint/probe 拆成"可选 TickFlow provider"设置 | M | 前端展示联动；需保留 `DATA_PROVIDER=tickflow` 仍可用的退路 | A2 |
| **A5** | 删 `tickflow.scheduler`（无引用）/ `tickflow.pools`（仅 tickflow fallback）/ `tiers.yaml` | S | scheduler 已确认零引用可直接删；pools/tiers 需先确认 tickflow provider fallback 不再需要 | A2/A4 |
| **A6** | 删 `TickFlowProvider` + `tickflow.client`（叶子） | S | **产品决策**：是否彻底不保留 `DATA_PROVIDER=tickflow`；保留则不删 | A1-A5 全部 |
| **A7** | provider capability 路由补全（审计 High-3）：financial/depth/minute 独立按能力选 provider，不再搭 daily/global | M | 当前全局 `DATA_PROVIDER=fquant_local` 时无碍；仅 per-capability 混切才需要——**可能 YAGNI**，取决于是否真要混源 | 无 |

### Track B — Trade Journal 延伸

| ID | 功能 | 工作量 | 关键风险 | 依赖 |
|---|---|---|---|---|
| **B1** | **Shadow Account**（从盈利 roundtrip 抽 if-then 规则→回放成影子组合→delta-PnL 归因） | XL | Vibe 源 ~3654 行；**门槛**：先确认 Trade Journal 行为诊断"够好到值得回放"；两个软接缝（见 CONTEXT.md）：fills 需重新上传解析或加字段、shadow(价格pnl) vs actual(total_pnl含分红)需先定分红口径 | Trade Journal MVP（✅） |
| **B2** | Trade Journal 增强：多账户合并 / 增量导入去重 / Hybrid LLM 叙事（聚合数字，opt-in） | M | 隐私红线：Hybrid 只送聚合、绝不送原始流水；多账户需持久化归一 fills（当前只存报告） | Trade Journal MVP（✅） |
| **B3** | 追涨港股覆盖：港股日线接入本地 parquet（当前港股在 TDX 磁盘按需读、未落 parquet，追涨对港股全缺，样本 65 只港股 uncovered） | M | 港股日线数据源/落盘路径；与 P6 港股链路衔接 | P6（✅） |

### Track C — Vibe 迁移候选剩余（C2-C13，去重 P 系列）

| ID | 功能 | 工作量 | 关键风险 | 依赖 |
|---|---|---|---|---|
| **C3** | A 股参考数据剩余源接 ext_presets：解禁→股东户数→两融→大宗→研报/EPS→新闻（**北向已剔除**，2024-08 停更，见 R7） | M（逐源 S） | 东财接口字段/限流会变；须 `trust_env=False` + host allowlist + 退避（抄 Vibe `HostThrottle`）；逐源黄金 fixture | P1（✅，同一 ext_presets 抽象） |
| **C2** | 研究假设 registry + run_card：假设生命周期(exploring/testing/validated/rejected/monitoring) + 回测 artifact/config/strategy hash + 证据 ledger | M | 与现有 strategy_cache 区分（那是缓存非研究资产）；本地 JSON store MVP 即可 | 无 |
| **C5** | 回测稳健性验证：walk-forward（默认）+ Bootstrap Sharpe CI + Monte-Carlo permutation（手动开）+ per-symbol/exit-reason + run_card | M | panel 回测已有 IC/IR/多空/Calmar（非从零），是"在较完整统计层加稳健性检验"；纯后处理低风险 | C2（run_card 复用） |
| **C4** | Alpha Zoo registry/manifest/compare/strict-bench + Alpha101 扩到 10 个 | L | pandas→Polars 翻译易错，**必须黄金对拍**（P2 已建方法论）；AST 只是元数据安全提取、非纯函数门禁，扩因子仍需自写副作用校验 | P2（✅） |
| **C13** | 技术形态识别（port Vibe `pattern_tool.py`，纯 pandas/numpy 峰谷/形态）→ 回测/个股分析页 | M | pandas→Polars/numpy 重写；形态定义主观，需固定口径 | 无（低耦合） |
| **C6** | Universal document/web reader：PDF/DOCX/XLSX/图片OCR + 网页→Markdown，喂 AI 分析 | M | **依赖较重**（`rapidocr_onnxruntime` ONNX，非 pytesseract）；SSRF（网页只允许公网 http/https）；原文不长期存储 | 无 |
| **C8** | MCP Server：把 panel 选股/回测/行情/梯队暴露为 MCP tools（复用 agent_tools.TOOLS） | M | 工具暴露边界 + 本地数据访问收紧 + 鉴权 | P7（✅ 骨架） |
| **C7** | Finance Skills 方法论库：A股相关 Markdown（eastmoney/sector-rotation/trade-journal/risk-analysis 等），AI prompt 按场景加载 | S | 筛掉美股/crypto/期权无关内容，避免污染 A 股定位；纯 Markdown 低风险 | C8（可选联动） |
| **C9** | 定时研究：扩"定时复盘"为"定时研究模板"（大盘/自选/策略池周报），先模板化不做自由 prompt | M | AI 调用成本 + 重复任务限额；APScheduler 已在项目内 | C2（研究资产落库） |
| **P4** | TDX 磁盘数据质量核对清单（Vibe `TDX_LOCAL_DATA_INTEGRATION.md`）：① volume=股/amount=元 量纲核对（对 `mapping.py` 与换手率单位假设）② 早期"对数复权负价"数据质量断言（panel 用 raw 重建优于直接丢弃）③ 港股 amount=0 边界 | S | 半天核对，纯断言加固；对刚上线的磁盘直读做数据质量兜底 | fquant_local（✅） |
| **C12** | Symbol search 增强：借 Eastmoney suggest 做补全（不迁移外部 screener，本地策略引擎为准） | S | 外部 suggest 与本地 instruments 口径不一致 | 无 |
| **C11** | 策略导出：只做 TDX/同花顺公式（不做 Pine/MT5），仅无状态日线信号 + 已有指标列 | M | 导出语义易与 panel 指标口径漂移；需限定 DSL 子集 | 无 |
| **C10** | Mandate Gate / Kill Switch **设计（ADR）**：单笔/敞口/杠杆/日次数硬上限 + 文件哨兵熔断 + fail-closed | S（仅设计） | **只出设计不实现**；涉及实盘资金，实现属交易桥接立项 | 无 |

### Track D — 明确不做 / 远期（存档，非本路线图排期）

| 项 | 理由 |
|---|---|
| LangGraph/Swarm agent 框架、IM 双向会话运行时 | 薄循环（P7）替代；YAGNI |
| 7 引擎 18 loader 多市场框架、券商连接器、期权/crypto | 打散 provider 抽象红线 / 偏离 A 股定位 |
| 实盘直连下单 | 高风险，独立立项，先 C10 设计 + 信号文件导出到 QMT/掘金 |

---

## 三、优先级与依赖关系

**排序原则**：①解真实用户阻断 > ②低成本高复利基建 > ③分析深度 > ④重投入功能。依赖先行。

```
A1(分钟K阻断) ──┐
                ├─► 独立 quick-win，无依赖，最先
C3(数据源) ─────┤   （逐源增量，随做随用）
C2(假设registry)┘
       │
       ▼
C5(回测验证) ◄── 复用 C2 run_card
A2/A3(去TickFlow解耦) ── 可与上并行（A3 纯机械）
       │
       ▼
C4(因子库) ◄── P2 方法论   C13(形态)   C6(reader)
       │
       ▼
B1(Shadow Account) ◄── 门槛：Trade Journal 诊断价值验证
       │
       ▼
C8(MCP)/C7(skills)/C9(定时研究) ◄── P7 骨架
       │
       ▼
C10/C11(交易桥接前置) ── 远期，独立立项
```

**关键依赖硬点**：
- A6（删 TickFlowProvider）依赖 A1-A5 全绿 + 产品决策"彻底弃 tickflow provider"。
- B1（Shadow Account）依赖"诊断够好"的价值验证，不是技术依赖——**门槛是产品判断，不是代码就绪**。
- C4/C13 依赖已建的黄金对拍方法论（P2），否则 pandas→Polars 翻译无保障。

---

## 四、推荐执行阶段

### Phase 0 — 固化（0.5-1 天，最先）
把本会话已实现但未提交的工作分块 commit，让基线可信、可回滚。
1. P1-P7 各自 commit（ext_presets/factor_zoo/optimizers/agent_tools/webhook）。
2. ETF 接入 + benchmark 回填 + 追涨精修 commit（provider 路由 / symbols 辨识 / pricepos）。
3. 审计文档 + 本路线图 + CONTEXT 软接缝 commit。
> 风险：`data/` 回填产物是运行态、不入 git；只 commit 代码与脚本。

### Phase 1 — 数据面拓宽 + 研究基建（1-2 周）
低成本、高复利、随做随用。
- **A1**（分钟K阻断，S，独立先行）
- **C3**（剩余 A 股数据源，逐源 S，按 解禁→股东户数→两融→大宗→研报EPS→新闻）
- **C2**（假设 registry + run_card，M）
- **C5**（回测稳健性验证，M，复用 C2）
- **P4**（TDX 磁盘数据质量核对，S，对本地直读做兜底加固）

### Phase 2 — 去 TickFlow 解耦（1-1.5 周，可与 Phase 1 并行）
- **A3**（repository rename，M，纯机械，随时可插）
- **A2**（capability 中性化，L）
- **A4→A5**（settings/health 去展示 + 删 scheduler/pools/tiers，M+S）
- **A7**（路由补全，M，若确需混源；否则记 YAGNI）
- **A6**（删 Provider/client，S，待产品决策）

### Phase 3 — 分析深度（1.5-2 周）
- **C4**（Alpha Zoo registry + Alpha101×10 黄金对拍，L）
- **C13**（技术形态识别，M）
- **C6**（doc/web reader，M，谨慎评估 OCR 依赖）

### Phase 4 — Trade Journal → Shadow Account（XL，有门槛）
- **B3**（追涨港股覆盖，M，补齐诊断盲区）
- **B1**（Shadow Account，XL）——**先做价值验证**：Trade Journal 上线后收集"行为诊断是否真的帮用户"，够好再启动；启动时按 CONTEXT 软接缝处理 fills/分红口径。
- **B2**（Trade Journal 增强，M，按需）

### Phase 5 — Agent / 集成（1-1.5 周）
- **C8**（MCP Server，M）
- **C7**（Skills 库，S）
- **C9**（定时研究模板，M）

### Phase 6 — 交易桥接前置（远期，独立立项）
- **C10**（Mandate/Kill-Switch 设计 ADR，S）
- **C11**（TDX/同花顺公式导出，M）
- 信号文件导出到 QMT/掘金（不直连下单）

---

## 五、风险总览（跨阶段）

| 风险 | 触及功能 | 缓解 |
|---|---|---|
| pandas→Polars 翻译语义漂移 | C4、C13 | 强制黄金对拍（同 fixture 双实现逐值比对，P2 已建） |
| 东财等外部接口字段/限流变动 | C3、C6、C12 | `trust_env=False` + host allowlist + `HostThrottle` 退避；逐源 fixture |
| 去 TickFlow 改动面广易漏 | A2、A3、A4 | A3 按 8 处导入方清单；A2 全量测试 + 保留兼容导入一版 |
| 真实交易/流水隐私 | B1、B2 | 纯统计诊断、Hybrid 只送聚合、原始文件用后即弃（已是 MVP 红线） |
| Shadow Account 成本被低估 | B1 | 已定 XL + 价值门槛；不为它提前改 MVP（软接缝留档兜底） |
| 重依赖引入（OCR/ONNX） | C6 | 第一阶段只做文本/Excel/PDF 文本层，OCR 单独评估 |
| 实盘资金安全 | C10 + 远期 | 只设计不实现；fail-closed + kill-switch 先行 |

---

## 六、单分支建议

若只开一个后续分支，建议 **Phase 0 固化 + A1（分钟K阻断）+ C3 首个数据源（解禁）**：全部低风险、独立、当天见效，且把"未提交基线"这个隐患先清掉。Shadow Account（B1）是最有产品共鸣的大功能，但应在 Trade Journal 实际使用验证价值后再启动，不宜作为下一个立即分支。
