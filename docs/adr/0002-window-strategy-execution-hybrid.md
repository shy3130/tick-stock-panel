# 0002 — 含窗口算子的策略走混合执行模型（T3）

- **状态**：已接受（2026-07-02）
- **相关**：[ADR-0001](0001-dsl-keeps-full-window-operator-catalog.md)、[策略 DSL 设计](../superpowers/specs/2026-07-02-strategy-dsl-and-fquant-datasource-design.md) Part A

## 背景

ADR-0001 决定 DSL 保留窗口算子。窗口算子在 `as_of` 当天求值需要每只 symbol 的 N 天历史，必须钉死"窗口何时被计算"。

执行现状（`app/strategy/engine.py`）：普通策略 `run(as_of)` 只加载单个交易日 enriched（快路径）；仅声明 `filter_history` 的策略走 `history_loader(as_of, lookback_days)` 加载历史窗口。选股 `run_all` 与盘中监控扫描都走单日快路径。

考虑过：
- **T1 运行时求值**：任何用到窗口的 DSL 在 `run` 时自动拖历史。→ 把历史加载引入选股/监控热路径，盘中每 tick 拖历史，性能敏感。
- **T2 注册时物化**：保存策略时把窗口子表达式物化成 enriched 列。→ 热路径仍单日，但 enriched 列集合随用户策略动态增长，污染回测 `load_panel` / DuckDB 的列假设。

## 决策

采用 **T3 混合模型**：
- **无窗口** 的 DSL → 单日快路径（`loader(as_of)`），与现状一致。
- **含窗口** 的 DSL → 标记 `requires_history`，`lookback` 由 IR 内所有 `window.n` 的最大值**自动推导**（作者不手写 `LOOKBACK_DAYS`），复用现有 `filter_history` / `history_loader` 历史加载路径。

## 后果

- ✅ 精确复用引擎已有双路径，改动面最小。
- ✅ enriched 列集合保持静态，回测/DuckDB 列假设不受用户策略影响。
- ✅ lookback 自动推导，消除手写 lookback 与真实窗口不一致的隐患。
- ⚠️ 含窗口策略在选股/监控中比无窗口策略更重（历史加载）；`run_all` 需按"是否含窗口 + 最大 lookback"分组共享历史（引擎已有共享 history 的先例）。
- ⚠️ 盘中监控若命中含窗口策略，需要历史窗口——需在监控侧确认可接受的刷新成本（后续 grill）。
