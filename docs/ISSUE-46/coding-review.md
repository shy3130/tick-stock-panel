# Issue #46 编码复核

实现包含 pinned factor panel、train/validation/test 路由、固定种子 random-neighbor/random-label placebo、成本后分池增量与严格 JSON envelope；核心 evaluator 不写文件、不联网、不接默认池。

独立 review 发现原边界只有 `neighbor_date < query_date`，当 `label_horizon>1` 时仍会让未完全兑现的 forward label 进入检索库。已改为 `neighbor_index + label_horizon < query_index`，事件新增 `label_available_date`，边界 guard、真实路由与两类 placebo 共用同一 purge 规则。新增成功路径测试及 horizon=3 的可得性测试。

显式导入替代 wildcard，Ruff F/E9 已清零；Pydantic `schema` 使用 alias，保持外部 JSON 契约且消除属性遮蔽警告。
