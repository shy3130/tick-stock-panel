# Issue #46 验证

- 六项研究与共享适配层定向回归：`140 passed`。
- MERA 专项覆盖 panel identity、train-only 标准化/标签/库、60/20/20、候选 K/距离、label realization purge、成本池、random-neighbor/random-label placebo、API contract。
- 80 日×30 标的成功路径逐邻居断言 `label_available_date < query_date`；horizon=3 回归同时验证 train/validation label 不跨入下一 split。`{A,B}->{A}` 回归确认等权池缩小会计入 1.0 的 L1 换手，而非误报零成本。
- Ruff changed-scope `--select F,E9`：All checks passed。
- 完整后端：`3690 passed, 3 skipped, 8 warnings`（138.55s）。
- 当前仅为日线代理；未声称复现分钟 MERA/MoE，也未写入默认池。
