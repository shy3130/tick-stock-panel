# ISSUE-29 实施波验证记录

> 日期：2026-08-29 · 基线：`workbench/feature/fstore-engine-duckdb-source` @ `7bf2982`
> 本文件记录主会话执行并核对的历史工程验证与真实 OOS 证据；verdict worktree 仅落档，不改研究代码。
> 单元/契约测试证据与真实 OOS 结论分开记录；收益数字只来自下述 immutable generation-pinned OOS 运行。

## 验证证据（主会话执行）

| 项目 | 结果 | 说明 |
|------|------|------|
| Focused identity/canonical/zuoyi 测试 | **20 passed** | markets pin identity、canonical publisher source_generations、zuoyi-defense 契约与判例 |
| 后端全量回归 | **3460 passed, 3 skipped, 8 warnings in 104.90s** | 最终全量 `backend` 套件 |
| Ruff F/E9 | **all checks passed** | 静态检查无未定义名/语法错误级问题 |
| 真实 current 冒烟 | `PublishedDailyMarketFactsReader.from_canonical_manifest` 抛 `FileNotFoundError`（pinned markets generation unavailable） | **strict pin 预期 fail-closed**：已发布 legacy canonical 无 expected markets hash/generation 组合即拒绝；下一次 verified canonical incremental/full publish 后自动可用 |
| 独立 coding review | 最终 **approve，无 blocker/major** | 20 项 finding 全部闭环，见 [coding-review.md](coding-review.md) |
| canonical schema v2 全历史发布 | **succeeded** | generation `20260829T002957-4b1bfcad`，17,230,945 行、5,680 标的；完整 canonical manifest SHA-256 见下节 |
| 真实 OOS | **status=`ok`，旧 verdict=`accepted`（已作废，待严格最佳基准门禁重跑）** | 确定性 25 标的、75 个完整 OOS segment（门槛 20）；机器摘要见 [oos-verdict.json](oos-verdict.json) |

## 真实 OOS 执行与 verdict

### Immutable provenance

- canonical generation：`20260829T002957-4b1bfcad`
- canonical manifest SHA-256：`0d5b5a457e7fa8c25bb047005b20cc6ca06ed19092f7ce20ba65f4604dfdd372`
- markets generation：`20260829T000704`
- markets manifest SHA-256：`a2a9d2b8208af33f4bcb66bcbe46a02ee836659c337deab4d0fd550ffead22a8`
- markets pin 校验：`manifest_sha256_match`
- 运行窗口：`2023-08-10` 至 `2026-08-28`，OOS 起点 `2025-02-25`；740 个 pinned market days；10 bps 成本。
- cohort：在 OOS 前一市场日可用候选中按 canonical symbol 升序冻结前 25 只；不得按结果换股。

### Fail-closed 记录

首次以 `2022-03-04` 为起点的预运行被 `000001.SZ` 在 `2022-07-19` 至 `2022-07-21` 缺少 exact pinned markets 行按设计阻断。最终窗口调整只依据 source coverage，未读取任何臂的收益或 verdict；未对缺失涨跌停事实做猜测或降级。

### OOS 结果

- 完整 OOS segment：`75`，冻结门槛 `20`。
- 旧 verdict：`accepted`（已作废）；旧实现用 max paired improvement 选择 `ma60_hold`，只击败最弱基准，违反 plan-v1/v2“相对最佳基准”。
- 修复门禁：四个预注册 baseline 必须各自达到最低 paired OOS 样本，随后以 paired mean 最小者识别 strongest baseline，只检验该 strongest baseline 的 bootstrap lower bound；不得混写为“所有 baseline CI 均须通过”。
- 历史 OOS 平均净收益：`buy_hold=0.007832`、`atr_chandelier_k3=-0.004929`、`ma20_hold=-0.003428`、`ma60_hold=-0.023821`、`zuoyi_defense=0.004366`、`zuoyi_atr_combo=0.005706`。该旧运行只能作为 bug 证据，修复后必须重跑，不能直接据此登记新 verdict。

## 结论

实现与历史 OOS 证据已核验；旧 verdict 因门禁 bug 作废。完成修复后，主会话须运行定向测试并在 immutable generations 上重跑 OOS，确认新的 verdict 后再关闭 Issue #29。
