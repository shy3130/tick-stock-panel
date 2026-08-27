# Issue #12 — 弱转强涨停事件因子

- Issue: https://github.com/wf2311/fm-workbench/issues/12
- 分支: `issue-12-price-event-production`
- 状态: `production-seam-verified`

已落地 production composite reader/API seam：canonical、markets PIT、#10 sparse minute、signal-year pinned callauction 组件固定并生成 composite manifest；PIT 使用 effective/available 双 09:25 Asia/Shanghai 门禁与 exact `ztj`，缺 sortable tick/order book/float 只返回空/None 并按事件分支删失。production reader 由 API 请求拥有并在 finally 级联关闭；历史信号若 publication 晚于 effective cutoff，预期 `pit_incomplete`，不伪造封板分类。

定向合同测试、静态错误检查、独立二次 Review 与真实 production reader smoke 均已完成；当前 generation 可构造四组件 composite manifest，历史/同日 PIT 因 09:25 publication boundary 正确返回 unavailable。
