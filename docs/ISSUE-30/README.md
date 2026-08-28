# ISSUE-30 日线开盘价锚定入场过滤研究（daily-open-anchor-filter）

> 状态：**实现及依赖集成完成／待 PR（review-v3 approve；主会话定向、全量与 Ruff 已通过；最终 review Approve）**。
> 日期：2026-08-28 · 基线：`7bf2982` · GitHub Issue：[wf2311/fm-workbench#30](https://github.com/wf2311/fm-workbench/issues/30)

## 这是什么

研究问题：「收盘确认信号之后，仅当价格仍处于最近一根已收阳线的开盘价锚附近/下方时才允许入场」这一入场位置过滤器，能否在期望收益不恶化的前提下降低止损触发率。原稿仅有单一案例，不作采信依据；本研究以 sealed 数据四臂对照（无过滤 / 原稿 / 反向 / 确定性随机）回答，结论只由 OOS 决定。

v1/v2/v3 均冻结为 A 股多头、日频收盘确认、下一可交易日开盘执行；盘中小周期因 sealed 分钟覆盖不足而 `unavailable`。权威契约见 [final-design.md](final-design.md)。

## 范围与非目标

- 代理信号 MA5/MA20 金叉；锚为 signal_date 之前最近收阳线 open；统一 pinned canonical 前复权口径，PIT 限价带在 raw 尺度计算后附加 flags。
- 四臂共享同一 signal universe；被过滤样本由 none 臂逐候选终态提供虚拟结局；IS/OOS 按 signal_date 分段，verdict 只读 OOS。
- 不做盘中、空头/期货/期权/融券、FVG/OB、前端、策略池/optimizer/Agent、真实交易、全市场生产回测或 `data/` 写入；不采信原稿收益主张。

## 文档索引

| 文档 | 作用 |
|------|------|
| [feasibility.md](feasibility.md) | 可行性、数据事实、缺口与 fail-closed 边界 |
| [plan-v1.md](plan-v1.md) | 初版契约（历史记录） |
| [review-v1.md](review-v1.md) | 首轮独立评审：5 项 finding，Reject |
| [plan-v2.md](plan-v2.md) | 首轮评审后的修订契约（历史记录） |
| [review-v2.md](review-v2.md) | 二次独立评审：3 项 major，Reject |
| [plan-v3.md](plan-v3.md) | 修复二次评审 M1–M3 的实施契约（已批准） |
| [review-v3.md](review-v3.md) | 最终门禁：Approve，附一项已处置 P2 |
| [final-design.md](final-design.md) | plan-v3 权威副本（已批准） |
| [coding-review.md](coding-review.md) | 独立 coding review 九项修复与 strict reader/依赖处置记录 |
| [verification.md](verification.md) | 主会话首验与集成后证据、最终 review 结论 |

## 代码落点（已实现，主会话验证已通过）

| 文件 | 作用 |
|------|------|
| `backend/app/services/daily_open_anchor_filter.py` | sealed reader 装配、raw PIT bands、锚点、四臂、execution ledger、candidate stats、IS/OOS verdict |
| `backend/app/api/research.py` | capability GET + factor evaluate POST（fail-closed 400/503 映射） |
| `backend/tests/services/test_daily_open_anchor_filter.py` | raw/adjusted corporate-action、T+1、终态 ledger、虚拟结局与统计夹具 |
| `backend/tests/api/test_daily_open_anchor_evaluate_api.py` | API、markets pin、fail-closed、reader 生命周期与边界校验 |

## 验收标准（照录 Issue #30，当前实现及依赖集成已完成）

- [x] `docs/ISSUE-30/` 完成可行性、两轮方案 review、最终设计与验证记录。
- [x] 锚点严格满足 `anchor_date < signal_date`，新锚切换时点唯一，可截断复算，无未来函数。
- [x] 四臂共享同一代理信号全集；保留/过滤样本与被过滤样本虚拟结局并列披露。
- [x] PIT 精确涨跌停事实缺失整单 fail-closed；T+1、停牌、跳空止损、费用滑点均生效。
- [x] 分层披露趋势/震荡、高开/低开、距锚距离，结论只由 OOS 决定。
- [x] 覆盖阳/阴线锚、高低平开、单边下跌、跳空穿越、新锚切换、无信号、停复牌、缺数据、随机确定性和涨跌停夹具。
- [x] 定向测试、后端全量回归与 Ruff F/E9 通过；独立 coding review 无 blocker/major。

实现及依赖集成已完成；最终 review Approve（无 blocker/major/minor），证据见 [verification.md](verification.md)。

## 红线

sealed-only；canonical 与 markets generation 均 immutable pin；raw PIT bands 只与 raw OHLC 比较，缺必需事实整单 fail-closed；execution ledger 不伪造成交；研究 payload 仅统计性结果，无订单/方向/仓位建议；不触碰 `data/`、`short_pool`、Agent 运行时。请求上限 symbols≤200、window≤370、signals≤1000；四臂逐候选调用成本必须披露。
