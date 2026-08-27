# Issue #16 可行性评估 — MACD(10/20/7) 逐日阶段研究

- 日期：2026-08-27
- 分支：`issue-16-macd-stages`
- 状态：定稿（结论：契约先行可行，数值实现本期不可行）

## 1. 问题定义

Issue #16 要求以**固定参数 MACD(10, 20, 7)** 为基础构建"逐日阶段"研究能力：

1. **参数冻结**：快线 EMA10、慢线 EMA20、信号线 EMA7，不做网格寻优（避免过拟合与不可复现）；
2. **逐日状态机**：阶段判定按市场日逐日推进，当日只允许消费当日及以前的数据；
3. **三层字段语义**：每行输出携带 `raw`（原始行情快照引用）、`pit`（point-in-time 读取上下文）、`generation`（数据构建代次）；
4. **T+1 可用性**：当日收盘后产生的阶段事件，次一市场日才可作为研究输入；
5. **OOS 协议**：评估必须包含样本内/样本外（IS/OOS）切分与分层呈现。

## 2. 现状盘点（代码事实）

| 能力 | 现状 | 位置 |
|---|---|---|
| MACD 指标计算 | 存在，但参数为标准 12/26/9，且为全历史一次性批处理（polars 整列 `ewm_mean`），非逐日推进 | `backend/app/indicators/pipeline.py` |
| 逐日状态机 | 不存在。信号列为全历史向量化派生（`signal_macd_golden` 等），无 as-of 边界、无逐日状态保持 | 同上 |
| PIT 读取 | 不存在按 as-of 市场日读取冻结快照的研究侧读取器；kline 读取面向"当前最新视图" | `backend/app/storage/repository.py` |
| generation 概念 | engine 侧存在 generation/publish 快照（FStore/TDX 客户端按已发布代次解析），但未暴露给研究 API | `backend/app/data_providers/fquant/` |
| T+1 语义 | 回测域存在（事件延迟语义），指标研究域不存在 | `backend/app/backtest/` |
| OOS 协议 | 回测有参数网格与稳健性模块，无面向"阶段研究"的 IS/OOS 切分协议 | `backend/app/backtest/robustness.py` 等 |

## 3. 关键差距（当前能力缺失，明确清单）

1. **状态机引擎缺失**：仓内没有任何组件按市场日维护 MACD 阶段状态并只消费 ≤ 当日数据。
2. **OOS 评估缺失**：无按 generation/日期边界的 IS/OOS 切分与分层报告。
3. **PIT 研究读取器不可用**：生产侧现成的按 as-of 读取冻结代次快照的读取器当前不可用，研究 API 无从取得带 generation 的冻结视图。
4. **10/20/7 参数变体不存在**：现有指标管线硬编码 12/26/9，无参数化能力。

## 4. 可行性结论

- **契约先行：可行。** API 形态、字段语义、状态机规格、T+1 与 OOS 协议可以先定稿，并以 fail-closed 端点暴露（HTTP 200 + `status="unavailable"` + 结构化 reasons），不产生任何数值。
- **数值实现：本期不可行。** 上述差距 1–3 均为新工程；差距 3 还依赖外部 generation 快照设施恢复可用。
- **决策**：本 issue 只交付
  - (a) 设计文档链：feasibility → plan-v1 → review-v1 → plan-v2 → review-v2 → final-design；
  - (b) fail-closed 服务 `backend/app/services/macd_stages.py` 与端点 `GET /api/research/macd-stages`；
  - (c) 定向测试与 `verification.md` 验证记录。

  **不改动**任何行情、数据、provider 代码，不触碰 `data/`，不接外部接口。

## 5. 风险

| 风险 | 缓解 |
|---|---|
| 把"批处理指标列"当作状态机实现（前视偏差伪装） | final-design §3 明确逐日递推定义；实现验收必须展示 as-of 截断下逐日重算一致性 |
| 读取器不可用时用"当前视图"顶替 PIT 数据（静默前视） | fail-closed 契约：PIT 读取器不可用即 unavailable，禁止任何回退 |
| 契约漂移（文档与端点字段不一致） | 测试锁定 schema 版本串与 reasons 集合 |
| 措辞越界（操作类词汇混入研究输出） | 新增内容禁词扫描纳入验收 |
