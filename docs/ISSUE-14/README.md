# Issue #14 — 量价序列突破因子

- Issue: https://github.com/wf2311/fm-workbench/issues/14
- 分支: `issue-14-universe-scd-forward`
- 状态: `first-real-generation-published-and-verified`

- 已交付：独立 `volume_breakout_v1` 状态机、研究 API、forward/OOS/成本/重叠诊断、event-date PIT universe identity seam，以及 immutable universe SCD collector/publisher/reader。
- SCD 生产契约已固定 exact published fstore pin、`trade_date(tdate,isopen,mkt,lastdate,nextdate)` calendar contract（`isopen=3`）、eligible v1、next-market-day effective、parent CAS/flock/fsync/path guard 和整 run fail-closed。首个真实 generation `20260827T153316Z-20c87c09f41cffb9` 已发布：collection day `2026-08-27` 保持 unavailable，`2026-08-28` 起生效，共 5903 个标的。
文档：feasibility.md、plan-v1.md、review-v1.md、plan-v2.md、review-v2.md、final-design.md、verification.md。
