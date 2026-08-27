# v2 评审

日期：2026-08-27 · 结论：有条件通过。

## 已满足

- raw 价与 `raw_open` 缺口明确；不从复权字段猜测原始实体。
- 实体/影线公式、2% 阈值、5 根窗口、严格小于破低点均可复算。
- 窗口必须完整，T+1 与 OOS 边界写入定义。
- reader、状态机、OOS 三重门禁；即使 reader 能力齐备，仍返回 unavailable。
- endpoint 无交易字段、无外部接口、无 `data/`、`short_pool` 或 Agent 依赖。

## 放行条件

实现后必须运行完整 `test_single_yang_no_break.py`、`py_compile`，并检查 diff
只包含本 issue 文件；`verification.md` 只能记录实际结果。reader 补齐后需另立设计
评审，不能通过改配置绕过本服务的状态机/OOS 门禁。
