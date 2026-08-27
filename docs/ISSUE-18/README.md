# ISSUE-18 单阳不破研究（single-yang-no-break）

> 状态：**production-ready；canonical schema v2 已发布并具备盘后增量发布链**。
> 日期：2026-08-27 · 集成分支：`issue-8-research-production`

## 这是什么

「单阳不破」是一个**纯研究型**价格行为假设：一根实体达标的 raw 价阳线出现后，
后续固定窗口内价格不跌破该阳线最低价。本目录把该假设的**定义一次性钉死**
（raw 价口径、实体/影线、不破低点语义、T+1..T+5 观察窗口、T+5 确认、
T+6 起评估与 OOS 约束），
并交付对应的服务模块与 research API 端点。

当前实现已具备 generation-pinned raw reader、T+5 确认/T+6 评估、证据、IS/OOS 与成本诊断。旧 canonical generation 缺原生 `raw_open` 时仍 unavailable；禁止从复权 `open` 反推。

## 文档索引

| 文档 | 作用 |
|------|------|
| [feasibility.md](feasibility.md) | 可行性盘点：数据面/能力面缺口，为什么只能 fail-closed |
| [plan-v1.md](plan-v1.md) | 初版实施计划 |
| [review-v1.md](review-v1.md) | 对 v1 的评审意见（R1–R6） |
| [plan-v2.md](plan-v2.md) | 逐条回应评审后的修订计划 |
| [review-v2.md](review-v2.md) | 复审结论：有条件通过 |
| [final-design.md](final-design.md) | **权威**：固定单阳定义、服务与 API 契约、测试矩阵、红线 |
| [verification.md](verification.md) | 实际验证记录（py_compile / focused tests / diff check） |

## 代码落点

| 文件 | 作用 |
|------|------|
| `backend/app/services/single_yang_no_break.py` | 固定定义、状态机、generation/raw 门禁与 OOS/成本诊断 |
| `backend/app/api/research.py` | capability GET + `POST /api/research/factors/single-yang-no-break/evaluate` |
| `backend/tests/test_single_yang_no_break.py` | 2% 边界、T+5/T+6、raw_open、generation、OOS tests |

## 红线

- 无交易语义：不下单、无持仓/仓位/止盈止损字段，payload 不含任何交易类键。
- 无外部接口：只读取 published canonical generation。
- 不触碰 `data/`、`short_pool`、Agent 运行时。
