# Issue #48 编码复核

- S1/S8/S9 分别构造 shared `Detection`/`DetectionEvidence`；S1 前缀 MACD/红柱段/新高、S8 严格三连阴、S9 原始前收/当日开盘 5% 等号边界均已核对。
- S1/S8 从下一日开盘执行；S9 仅限已有持仓并以当日开盘执行；N=1 为执行日收盘。
- 每信号独立报告卖飞与规避深度；没有冻结 OOS 结构化基线时 verdict 明确 unavailable。
- minute capability 与 approximation guard fail-closed；S2-S7/S10 不接受日线近似。
- API capability 与日线 production evaluator 已接入；无方向字段、订单或交易写入。

独立 review 未报告本 Issue 的 P0/P1/P2 问题。
