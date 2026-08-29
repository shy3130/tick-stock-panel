# ISSUE-29 左一K线防守位移动止盈研究（zuoyi-defense）

> 状态：**真实 OOS 已完成，冻结门禁 verdict=`accepted`；待本次 verdict 文档 PR 合并后关闭 Issue**。
> 日期：2026-08-29 · 基线：`7bf2982` · GitHub Issue：[wf2311/fm-workbench#29](https://github.com/wf2311/fm-workbench/issues/29)

## 这是什么

「左一K线防守位」是一个**持仓/离场 overlay 研究因子**：持仓期间以上涨结构中
「中位线向左第一根不被完全包含的 K 线」的低点作为防守位，收盘跌破防守位则
于下一可交易日 open 离场。原稿只有成功案例，本项目**不把减少卖飞、控制回撤
或收益提升当成事实**——结论只由样本外（OOS）对照实验决定，接受 `rejected`。

## 范围（v1 冻结）

- 仅日线；数据只读**单一 generation-pinned sealed canonical history**（前复权
  OHLC 为主口径，`raw_*` 仅作证据）。
- 一次性冻结：上涨状态确立、中位线窗口、同价高点 tie-break、包含关系、
  左一K线搜索、防守位、创新高重算、收盘破位语义。
- 收盘确认 → **下一可交易日 open 执行**；T+1、停牌、一字跌停、跳空穿越按
  真实可达性处理（镜像 `BacktestEngine` 语义，但**不改引擎**）。
- 六臂对照（buy-and-hold / ATR 吊灯 / MA20 / MA60 / 左一防守 / 左一+ATR 复合）
  共用同一**冻结入场集**；verdict 仅由 OOS 决定。
- 返回结构化证据、删失、provenance、成本后指标、卖飞率、破位后下跌深度，以及
  明确的 `accepted / rejected / unavailable`。

## 非目标

- 1 小时 / 15 分钟级别（当前 sealed 分钟链路实测仅 3 标的 × 30 交易日，见
  [feasibility.md](feasibility.md)）。
- 修改生产回测引擎（结构化移动止盈引擎集成留作研究 validated 后的独立后续 Issue）。
- 前端 UI 改动、接入真实交易/策略池/监控/生产调度、写入 `data/`。
- 全市场结果不达标时为凑数放宽规则（参数集冻结，禁止无限调参）。

## 验收标准（对应 Issue 正文清单）

1. `docs/ISSUE-29/` 完成可行性、两轮方案 review、最终设计、实施 review 与验证记录（见 [verification.md](verification.md) 与 [coding-review.md](coding-review.md)）。
2. 防守位可在信号时点**截断复算**，无未来函数；同价高点、包含关系和窗口定义唯一。
3. 后端 capability / evaluate 契约 fail-closed；证据含左一索引、防守位、
   信号时间与实际执行信息。
4. 六臂共享 common entry set，IS/OOS 分离；样本外无稳定增量时标记 `rejected`。
5. 测试覆盖：同价高点、完全/等点包含、下影触碰、次日收回、跳空、震荡、
   停复牌、一字跌停、除权、缺数据、**截断不变性**夹具。
6. 定向测试、后端全量回归与 Ruff F/E9 通过；独立 coding review 无 blocker/major
   （真实证据见 [verification.md](verification.md)）。

## 文档索引

| 文档 | 作用 |
|------|------|
| [feasibility.md](feasibility.md) | 可行性盘点：数据事实、可复用模块、缺口、非目标、风险 |
| [plan-v1.md](plan-v1.md) | v1 初版冻结契约（已由 review-v1 拒绝，保留作审查存档） |
| [review-v1.md](review-v1.md) | 独立审查：9 项 finding，结论 `incorrect` / reject |
| [review-v2.md](review-v2.md) | 二次独立复审：6 项 finding，结论 `incorrect` / reject |
| [plan-v3.md](plan-v3.md) | 修复二次复审剩余项并补齐 response schema，已批准 |
| [review-v3.md](review-v3.md) | 最终门禁复审：R8 schema major 追加不变式，已修正并批准 |
| [verification.md](verification.md) | 主会话最终验证证据、strict pin smoke 与上线前置条件 |
| [coding-review.md](coding-review.md) | 独立 coding review 20 项 finding 闭环与最终 identity 证据 |
| [oos-verdict.json](oos-verdict.json) | 真实 OOS 请求、immutable provenance、六臂均值、verdict 与限制的机器可读摘要 |

## 代码落点（实现波）

| 文件 | 作用 |
|------|------|
| `backend/app/services/zuoyi_defense.py` | v3 固定定义、状态机、六臂逐段评估与 approved response |
| `backend/app/api/research.py` | capability GET 与 evaluate POST 接线 |
| `backend/app/data_providers/fquant/daily_market_research.py` | immutable markets generation reader、PIT raw/band facts 与 identity 校验 |
| `backend/app/services/canonical_history.py` | canonical full/incremental publisher 与 snapshot identity pin |

## 真实 OOS 收口（2026-08-29）

- canonical generation `20260829T002957-4b1bfcad`，canonical manifest SHA-256 `0d5b5a457e7fa8c25bb047005b20cc6ca06ed19092f7ce20ba65f4604dfdd372`。
- markets generation `20260829T000704`，markets manifest SHA-256 `a2a9d2b8208af33f4bcb66bcbe46a02ee836659c337deab4d0fd550ffead22a8`，校验模式 `manifest_sha256_match`。
- 确定性 25 标的 OOS 得到 75 个完整 segment（门槛 20）；冻结规则相对 `ma60_hold` 得到 `accepted`。
- 该 verdict 不表示六臂全局最优：`buy_hold` OOS 均值 `0.007832`，高于 `zuoyi_defense` 的 `0.004366`；结果不触发生产策略、默认短线池或 Agent 排序变更。

机器可读摘要见 [oos-verdict.json](oos-verdict.json)，执行与首个 source-coverage 阻断记录见 [verification.md](verification.md)。

## 红线

- 只走 canonical sealed 数据链路；单一 generation 内计算，禁跨 generation 合并。
- canonical 与 markets identity 必须同时固定 generation + 完整 manifest SHA-256。
- 严格 PIT：不引用 T 时刻未完成 bar；禁用非严格 PIT 的 `turnover_rate` 列。
- fail-closed：reader/列缺失返回 `unavailable` + 原因，绝不降级猜测。
- A 股 T+1：入场日不出场；停牌/一字跌停 pending 至首个可卖日。
- 不写 `data/`；不进策略池/监控；无交易语义字段（不下单、无仓位）。

## 建议分支

`issue-29-zuoyi-defense`（自 `workbench/feature/fstore-engine-duckdb-source` 创建，
先例 `issue-18-single-yang`）。
