# 0001 — 策略 DSL 保留完整窗口/状态算子目录

- **状态**：已接受（2026-07-02）
- **相关**：[策略 DSL 设计](../superpowers/specs/2026-07-02-strategy-dsl-and-fquant-datasource-design.md) Part A（A4）

## 背景

调研发现：18 个内置策略的 `entry`/`filter` 全部只引用**预计算列**（`signal_*`、`consecutive_limit_ups`、`annual_vol_20d` 等）+ 阈值/跨列比较，**当前 0 个策略在选股表达式里需要即时窗口**。真正的窗口/状态计算（连板递推、60 日新高低、涨停判定、年化波动）都在 indicators 流水线预计算成列。

因此存在权衡：把 `rolling_*` / `cross_up` / `consecutive_true` / `cs_rank` / `cs_qcut` 等窗口算子放进跨语言 IR，会带来最大的一块 Python/Go 语义对齐负担（黄金测试几乎全压在这些算子），换来的却是当前无人使用的"策略内联造窗口"能力。

考虑过的替代：
- **(b) 收窄**：只留 `shift` + `cross_up/down`，其余窗口移回流水线预计算。
- **(c) 纯无状态**：窗口算子一个不留，DSL 只做预计算列上的布尔/算术。

## 决策

保留 **A4 的完整窗口/状态算子目录**（(a)）。策略 DSL 具备内联表达窗口/状态的一等能力，不依赖"先改流水线加列再重算全量"才能引入新窗口逻辑。

## 后果

- ✅ 策略作者/AI 可直接在 DSL 里表达新窗口逻辑，无需触碰 indicators 流水线与全量重算。
- ⚠️ IR 的跨语言表面积最大化；Python 与（未来）Go compiler 必须对每个窗口算子逐条对齐语义（ddof、分段、分箱并列、null、排序、跨 symbol 边界）。**A5 的 IR 规范 + 黄金测试是唯一防线，必须先于任一后端 compiler 落地。**
- ⚠️ 触发"窗口算子何时求值/是否强制加载历史窗口"的执行模型问题（见后续 ADR / grill）。
- 递归类算子（EMA/MACD/KDJ/RSI）仍**不进** DSL，继续走流水线预计算。
