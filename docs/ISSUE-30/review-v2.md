# ISSUE-30 review-v2：对 plan-v2 的二次复审

> 结论：**Reject（overall_correctness = incorrect，confidence 0.99）**。
> 审阅对象：[plan-v2.md](plan-v2.md) · 上轮：[review-v1.md](review-v1.md) · 关联：[Issue #30](https://github.com/wf2311/fm-workbench/issues/30) · [README](README.md)
> 修复记录：[plan-v3.md](plan-v3.md)；权威整合稿见 [final-design.md](final-design.md)（状态：待最终复审）。

## 总体意见（忠实转写）

> Reject：R1 仅部分解决（raw PIT 限价与前复权面板的比较尺度未统一），R2 与 R3 仍被现有撮合器的逐候选结局和 T+1 入场路由所阻断。R4 的按信号日 IS/OOS 分段与 R5 的候选样本统计收口已具备可实现契约，但不足以抵消前三项 major。

## Findings

### M1 Capture post-entry outcomes per candidate（priority 1，confidence 0.99）

位置：`docs/ISSUE-30/plan-v2.md` 第 61–86 行。

> The proposed ledger is finalized before `simulate_independent_candidates` runs and records only entry prechecks (lines 61–78), but an entry that passes those checks can still produce no `TradeRecord` when every later sale is blocked or no future bar exists. The engine only increments aggregate `sell_suspended`/`sell_limit_down`/`sell_no_future` counters on those paths (`engine.py:1008–1017, 1169–1173`), so neither the required aggregate check nor the ledger can assign the none arm's exact blocked/censored outcome to the `(symbol, signal_date)` needed by the virtual join. This leaves R2 unresolved and can misclassify or make unauditable filtered candidates; extend the engine result with a per-candidate terminal outcome (including post-entry failures), or define a service-owned one-to-one outcome collector that receives it.

实现者核实（基线 7bf2982）：属实。卖出阻塞在 `_try_close`（`engine.py:1008-1017`：`pending_exit`/`blocked_exit_days`/`_count(block_reason)`）；数据耗尽未平仓为 `sell_no_future`（`engine.py:1169-1171`）或强制 `end` 平仓（`engine.py:1172-1173`）；均为聚合计数，无逐候选记录。

### M2 Define a matcher path that consumes the locked T+1 index（priority 1，confidence 0.99）

位置：`docs/ISSUE-30/plan-v2.md` 第 90–98 行。

> Section 3 requires the service to lock an executable T+1 row before invoking the engine, while the still-effective v1 configuration requires `entry_fill="open_t+1"`. That engine contract treats every `entries` bit as a signal-day bit and unconditionally shifts it to the following same-symbol row (`engine.py:815–820`); it has no mode that consumes an already locked execution index. Passing the specified `engine_entry_index` therefore executes on T+2, while passing the signal index continues to rely on precisely the adjacent-row shift the plan forbids. R3 remains unresolved unless the plan specifies an engine/API change that accepts fixed execution indices (or an equivalent adapter with a non-shifting entry fill while preserving the signal date) and tests it against a nonconsecutive-symbol panel.

实现者核实：属实。`entry_fill=open_t+1` 的 shift 为 `ent[1:] = ent_raw[:-1] & same_prev_symbol`（`engine.py:815-820`），引擎只有信号日位语义。

### M3 Compare limit bands on the raw price scale（priority 1，confidence 0.98）

位置：`docs/ISSUE-30/plan-v2.md` 第 42–49 行。

> The rebuilt `pre_close` and published limit prices in the markets snapshot are raw prices, but the still-effective panel contract supplies the matcher with forward-adjusted OHLC and says raw values are evidence-only (`plan-v1.md:47–50`). Comparing adjusted `high`/`low` directly with raw `upper`/`lower` at line 49 yields false limit flags for historical bars affected by corporate actions, changing whether a one-price board can be bought or sold. R1 is therefore only partial: require the service to compare raw high/low with the raw bands and attach the resulting booleans to the adjusted panel (or convert both bands consistently to the adjusted scale), with a corporate-action fixture proving the flags remain correct.

## 评审后处置

- v2 保留为历史记录，不就地修改；M1/M2/M3 修复全部落位 [plan-v3.md](plan-v3.md)；v2 的 R4（IS/OOS 分段）与 R5（candidate-sample 统计）经本轮确认可实施，继续有效。
- [final-design.md](final-design.md) 为 plan-v3 的权威整合稿，状态"待最终复审"；通过最终复审（review-v3）后才允许进入实现波次。
