# v1 评审

日期：2026-08-27 · 结论：退回修订。

## 发现

- **R1 破低点歧义**：必须说明后续 low 等于形态 low 时是否算破；否则实现不可复算。
- **R2 raw 价缺口遗漏**：现有 enriched 只有 raw_close/raw_high/raw_low，没有 raw_open；不能用前复权 OHLC 或未经证明的比例反推实体。必须显式记录 production generation-pinned/PIT reader 缺失。
- **R3 窗口前视风险**：尾部窗口未完成时不能把暂时未破视为已确认。
- **R4 T+1 缺失**：形态日 T 收盘后才能确认，任何评估起点必须是 T+1。
- **R5 开放条件不足**：仅等待 reader 会使 reader 补齐后静默产出结果；状态机和 OOS 未实现时仍必须 unavailable。
- **R6 越界风险**：不能添加订单、持仓、仓位、止盈止损等交易语义，也不能接外部数据、`short_pool` 或 Agent。

## 必须修订

定义、常量、响应白名单和 focused tests 均应显式覆盖 R1–R6。
