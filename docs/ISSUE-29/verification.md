# ISSUE-29 实施波验证记录

> 日期：2026-08-28 · 基线：`workbench/feature/fstore-engine-duckdb-source` @ `7bf2982`
> 本文件由主会话提供真实验证证据后落档；本波仅更新文档，未运行任何命令、未改代码。
> 单元/契约测试证据不等于真实 OOS 收益结论——红线不变：未跑出真实 OOS 结果前不写任何收益结论。

## 验证证据（主会话执行）

| 项目 | 结果 | 说明 |
|------|------|------|
| Focused identity/canonical/zuoyi 测试 | **20 passed** | markets pin identity、canonical publisher source_generations、zuoyi-defense 契约与判例 |
| 后端全量回归 | **3460 passed, 3 skipped, 8 warnings in 104.90s** | 最终全量 `backend` 套件 |
| Ruff F/E9 | **all checks passed** | 静态检查无未定义名/语法错误级问题 |
| 真实 current 冒烟 | `PublishedDailyMarketFactsReader.from_canonical_manifest` 抛 `FileNotFoundError`（pinned markets generation unavailable） | **strict pin 预期 fail-closed**：已发布 legacy canonical 无 expected markets hash/generation 组合即拒绝；下一次 verified canonical incremental/full publish 后自动可用 |
| 独立 coding review | 最终 **approve，无 blocker/major** | 20 项 finding 全部闭环，见 [coding-review.md](coding-review.md) |

## 结论

- 实现与验证契约全部满足：canonical sealed 链路、immutable markets pin identity（`{generation, manifest_sha256}`）、fail-closed 语义、六臂对齐、paired OOS bootstrap、closed 枚举与 response 不变式均有测试背书。
- 真实生产 current 因 legacy canonical 缺少可验证 markets pin 而诚实 `unavailable`，属设计内行为；升级路径已由 publisher 改造保障（增量即继承刷新 markets/tdx identity）。
- 后续动作：创建 PR 进入 review；真实 OOS 运行留待 PR 合并后的独立研究波，不在本波范围。
