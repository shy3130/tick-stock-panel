# 实施计划 v2

日期：2026-08-27

1. 固定 `SINGLE_YANG_DEFINITION`：raw OHLC；`close > open`；实体、上/下影线公式；实体占开盘价至少 2%；窗口 5 根完整后续日 K；后续 `low < anchor_low` 才算破，等于不算破。
2. 纯函数只接受显式 bars，不读取 provider、DuckDB、Parquet 或 `data/`；窗口不完整不确认。
3. 文档固定 T+1：T 收盘确认，评估从 T+1 起；正式样本外统计必须有独立 OOS 数据集。
4. capability 与研究入口恒定 fail-closed，原因至少包括 PIT reader 缺失、状态机未实现、OOS 未实现；reader 将来补齐也不得提前开放。
5. API 返回 200 + `status=unavailable` 的研究契约载荷；只允许 status/reasons/definition/note 等研究字段，不引入交易语义。
6. focused tests 锁定等低点、完整窗口、raw 定义和恒定 unavailable；diff 检查禁止 `data/`、`short_pool`、Agent 变更。
