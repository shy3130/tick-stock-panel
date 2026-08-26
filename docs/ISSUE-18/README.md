# ISSUE-18 单阳不破研究（single-yang-no-break）

> 状态：**unavailable（fail-closed）** —— 本期只交付诚实的不可用契约与固定定义，不产出任何可用信号。
> 日期：2026-08-27 · 分支：`issue-18-single-yang` worktree

## 这是什么

「单阳不破」是一个**纯研究型**价格行为假设：一根实体达标的 raw 价阳线出现后，
后续固定窗口内价格不跌破该阳线最低价。本目录把该假设的**定义一次性钉死**
（raw 价口径、实体/影线、不破低点语义、观察窗口、T+1/OOS 约束），
并交付对应的服务模块与 research API 端点。

**当前结论：研究能力不可用（unavailable）**，原因见下。即使数据 reader 将来补齐，
在研究状态机与 OOS 协议落地之前，服务仍必须返回 unavailable（双保险设计）。

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
| `backend/app/services/single_yang_no_break.py` | 定义常量 + 纯函数检测 + fail-closed 研究入口（无 IO） |
| `backend/app/api/research.py` | `GET /api/research/single-yang-no-break`（200 + unavailable 载荷） |
| `backend/tests/test_single_yang_no_break.py` | focused tests：语义锁定 + fail-closed 契约 |

## 红线（本期明确不做）

- 无交易语义：不下单、无持仓/仓位/止盈止损字段，payload 不含任何交易类键。
- 无外部接口：不连任何 HTTP/DB，纯本地纯函数。
- 不触碰 `data/`、`short_pool`、Agent 运行时。
- 不提交（无 git commit）。
