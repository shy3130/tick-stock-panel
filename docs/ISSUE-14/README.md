# Issue #14 — 量价序列突破因子

- Issue: https://github.com/wf2311/fm-workbench/issues/14
- 分支: `issue-14-volume-breakout`
- 状态: `event-engine-ready; blocked-by-pit-universe-history`

- 已交付：独立 `volume_breakout_v1` 状态机、研究 API、forward/OOS/成本/重叠诊断和 focused tests。
- generation-pinned reader 与版本化 canonical calendar 已具备；历史 PIT eligible-universe（含 available_at）仍无真实工件，生产调用继续 unavailable，禁止从 bars 或当前 universe 推导。
文档：feasibility.md、plan-v1.md、review-v1.md、plan-v2.md、review-v2.md、final-design.md、verification.md。
