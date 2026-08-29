# Issue #46 编码复核

实现包含 pinned factor panel、train/validation/test 路由、固定种子 random-neighbor/random-label placebo、成本后分池增量与严格 JSON envelope；核心 evaluator 不写文件、不联网、不接默认池。

独立 review 发现原边界只有 `neighbor_date < query_date`，当 `label_horizon>1` 时仍会让未完全兑现的 forward label 进入检索库。已改为 `neighbor_index + label_horizon < query_index`，事件新增 `label_available_date`，边界 guard、真实路由与两类 placebo 共用同一 purge 规则。新增成功路径测试及 horizon=3 的可得性测试。

PR #51 Codex review 又发现 validation 末端 label 跨入 test，以及池缩小时 overlap/new-size 算法漏记清仓成本。当前 train、validation、test 查询与 train label/基线选择均按 split end purge；换手改为逐腿比较新旧等权权重的 L1 变化，并增加跨窗与 `{A,B}->{A}` 回归。显式导入替代 wildcard，Pydantic `schema` 使用 alias，保持外部 JSON 契约。
