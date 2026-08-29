# Issue #45 验证

- 六项研究与共享适配层定向回归：`136 passed`。
- 独孤 detector/evaluator/API 覆盖双参数族、M3 20 日涨幅、T+1、双边成本、一字板 reachability、同父事件基线、market-facts 故障归因与 JSON contract。
- Ruff changed-scope `--select F,E9`：All checks passed。
- 完整后端：`3686 passed, 3 skipped, 8 warnings`（132.55s）；warning 均为既有 Polars sortedness/deprecation/performance warning。
- 未把小样本或无事件的真实数据运行冒充 accepted/rejected；研究结果仍由冻结 OOS 样本门槛决定。
