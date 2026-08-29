# ISSUE-29 实施波验证记录

> 日期：2026-08-29 · strict evaluator source：`c2d90e1e93ba70087ac048cebd70c01e15d3f804`
> 本文件记录主会话执行并核对的工程验证、review bug 修复与 immutable generation-pinned 真实 OOS 证据。
> 单元/契约测试证据与收益结论分开记录；最终 verdict 只来自修复后 fresh-process OOS。

## 验证证据（主会话执行）

| 项目 | 结果 | 说明 |
|------|------|------|
| Focused identity/canonical/zuoyi 测试 | **20 passed** | markets pin identity、canonical publisher source_generations、zuoyi-defense 契约与判例 |
| 后端全量回归 | **3460 passed, 3 skipped, 8 warnings in 104.90s** | 最终全量 `backend` 套件 |
| Ruff F/E9 | **all checks passed** | 静态检查无未定义名/语法错误级问题 |
| 真实 current 冒烟 | `PublishedDailyMarketFactsReader.from_canonical_manifest` 抛 `FileNotFoundError`（pinned markets generation unavailable） | **strict pin 预期 fail-closed**：已发布 legacy canonical 无 expected markets hash/generation 组合即拒绝；下一次 verified canonical incremental/full publish 后自动可用 |
| 独立 coding review | 最终 **approve，无 blocker/major** | 20 项 finding 全部闭环，见 [coding-review.md](coding-review.md) |
| canonical schema v2 全历史发布 | **succeeded** | generation `20260829T002957-4b1bfcad`，17,230,945 行、5,680 标的；完整 canonical manifest SHA-256 见下节 |
| 修复后真实 OOS | **status=`ok`，verdict=`rejected`** | 确定性 25 标的、75 个完整 OOS segment（门槛 20）；strongest baseline=`buy_hold`；机器摘要见 [oos-verdict.json](oos-verdict.json) |
| 最佳基准 review-fix focused | **9 passed in 2.79s** | strongest-baseline reject、strongest pass、service/API 契约 |
| 修复后后端全量回归 | **3534 passed, 3 skipped, 8 warnings in 176.47s** | warning 仅来自既有 Polars 路径 |
| 修复后 Ruff F/E9 | **all checks passed** | evaluator 与新增回归测试 |
| 独立二次复核 | **代码 approved；文档 P2 已修正** | strongest baseline、paired 样本与三态正确；文档已区分“全 baseline 样本”与“strongest baseline CI” |

## 真实 OOS 执行与 verdict

### Immutable provenance

- canonical generation：`20260829T002957-4b1bfcad`
- canonical manifest SHA-256：`0d5b5a457e7fa8c25bb047005b20cc6ca06ed19092f7ce20ba65f4604dfdd372`
- markets generation：`20260829T000704`
- markets manifest SHA-256：`a2a9d2b8208af33f4bcb66bcbe46a02ee836659c337deab4d0fd550ffead22a8`
- markets pin 校验：`manifest_sha256_match`
- evaluator source tree：`c2d90e1e93ba70087ac048cebd70c01e15d3f804`
- evaluator blob SHA-1：`0c266dea7efeb7467963d7f79b82086f59421a8f`
- 执行模式：Issue #29 verdict worktree 的 fresh Python process；evaluator 与 repository 文件均无 dirty diff。
- 运行窗口：`2023-08-10` 至 `2026-08-28`，OOS 起点 `2025-02-25`；740 个 pinned market days；10 bps 成本。
- cohort：`2025-02-24` 存在 canonical bar 的 symbol 升序前 25 只；不得按结果换股，实际列表见 [oos-verdict.json](oos-verdict.json)。

### Fail-closed 记录

首次以 `2022-03-04` 为起点的预运行被 `000001.SZ` 在 `2022-07-19` 至 `2022-07-21` 缺少 exact pinned markets 行按设计阻断。最终窗口调整只依据 source coverage，未读取任何臂的收益或 verdict；未对缺失涨跌停事实做猜测或降级。

### OOS 结果

- 完整 OOS segment：`75`，冻结门槛 `20`；四个预注册 baseline 均达到最低 paired 样本。
- strongest baseline：按 paired mean 最小识别为 `buy_hold`；只对该 strongest baseline 检验 paired bootstrap lower bound。
- 最终 verdict：`rejected`，规则 `paired bootstrap seed=42 rounds=500 shows no stable increment vs strongest baseline buy_hold`。
- OOS 平均净收益：`buy_hold=0.007832`、`atr_chandelier_k3=-0.004929`、`ma20_hold=-0.003428`、`ma60_hold=-0.023821`、`zuoyi_defense=0.004366`、`zuoyi_atr_combo=0.005706`。
- 旧实现用 max paired improvement 选择 `ma60_hold` 得到的 `accepted` 已作废；`rejected` 表示未证明相对冻结最强基准的稳定增量，不代表所有用途下必然无效。

## 结论

最佳基准门禁、定向测试、独立复核与修复后真实 OOS 均已完成。最终 verdict=`rejected`，不做生产提升；Issue #29 可在 PR #42 合并并回写最终证据后关闭。
