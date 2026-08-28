# ISSUE-30 review-v3：最终门禁复审

> 结论：**Approve（overall_correctness = correct，confidence 0.98）**——附一项 P2 minor，处置见下。
> 审阅对象：[plan-v3.md](plan-v3.md) · 上轮：[review-v2.md](review-v2.md) · 关联：[Issue #30](https://github.com/wf2311/fm-workbench/issues/30) · [README](README.md)
> 权威契约：[final-design.md](final-design.md)（本波起为已批准状态）

## 总体意见（忠实转写）

> Approve：上轮 R1–R3 的 raw PIT 尺度、逐候选终态账本和精确 T+1 路由均已与现有 reader/engine 契约闭合；R4/R5 继续成立。存在一项不阻断实现的 P2 测试/契约一致性问题：完整 horizon 预删失使 engine 侧 sell_no_future 不可达，应改为验证 horizon_data_gap。

## Finding

### P2 Align the sell-no-future contract with horizon censoring（priority 2，confidence 0.98）

位置：`docs/ISSUE-30/plan-v3.md` 第 79–86 行（关联映射行 57–60 与测试行 99）。

> The design censors every candidate before the engine call when any of the 16 required T+1-through-T+16 market days is absent (lines 81–86), while the inherited matcher always closes a fully present 15-day holding window by `max_hold` on T+16 (`engine.py:1142–1145`). Consequently an engine-invoked candidate cannot reach the claimed `sell_no_future` terminal state, yet the ledger mapping and test matrix require that state to be emitted (lines 57–60, 99). Remove that unreachable post-entry case from this design and test it as the pre-call `horizon_data_gap` censor instead, or explicitly relax the horizon gate and define how the partial holding is censored.

实现者核实（基线 7bf2982）：属实。服务在调用前对 required horizon（T+1..T+1+15 共 16 个 market days）统一 `horizon_data_gap` 预删失（plan-v3 §3.2），engine 侧仅当 `last_idx == entry_idx` 才产生 `sell_no_future`（`engine.py:1169-1171`）；horizon 完整时该条件不可达，且 15 日满仓持有会在 T+16 被 `max_hold` 收口。

## 处置（已并入本波修订）

采纳 P2 第一方案（不放宽 horizon 门）：

1. [plan-v3.md](plan-v3.md) 与 [final-design.md](final-design.md) 删除 `sell_no_future` 作为可达 terminal outcome 的要求与测试；§2.2 以显式「不可达说明」行保留契约依据（连续 horizon 下 `last_idx ≠ entry_idx`，故不产生该终态）。
2. 保留 post-entry `sell_suspended`/`sell_limit_down`（含 `pending_exit`/`blocked_exit_days`）的单候选终态映射不变。
3. horizon 缺 bar 仍为调用前 `horizon_data_gap` censor；测试矩阵改为覆盖该预删失路径，不再断言 `sell_no_future` 输出。

review-v1（5 findings）、review-v2（3 majors）至本轮全部闭合；`final-design.md` 自本波起为实现唯一依据。
