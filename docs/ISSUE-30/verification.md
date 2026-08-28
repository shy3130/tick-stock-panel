# ISSUE-30 验证记录（verification）

关联：[Issue #30](https://github.com/wf2311/fm-workbench/issues/30) · [README](README.md) · [coding-review.md](coding-review.md) · [final-design.md](final-design.md)  
本文件记录主会话执行的实际验证证据；#30 worktree 自身按主会话指令不运行测试/构建。日期：2026-08-28 · 基线：`7bf2982`。

## 主会话验证证据

| 项 | 命令范围 | 结果 |
|---|---|---|
| 定向测试（reader / service / API） | `tests/data_providers/test_daily_market_research*.py`、`tests/services/test_daily_open_anchor_filter.py`、`tests/api/test_daily_open_anchor_evaluate_api.py` | **22 passed** |
| 后端全量回归 | `backend` 全量测试套件 | **3475 passed, 3 skipped, 8 warnings，106.54s** |
| Lint | Ruff（F/E9 规则组） | **passed** |

以上结果由主会话（Main）在其合并环境中执行并回传；#30 worktree 按纪律未重复执行。

## coding review 终审状态

- 独立 coding review 此前共 9 项 findings，已全部解决，记录见 [coding-review.md](coding-review.md)。
- 最后 2 项由两件事收口：
  1. **strict reader**：#29 严格 hash pin 版 `daily_market_research.py`（legacy string pin → `pin_identity_verified()` false；mapping pin 必须含 `generation` + `manifest_sha256`），#30 已逐字同步并强制 identity gate；
  2. **Issue #29 canonical publisher 依赖**：#30 不复制 `canonical_history` publisher，PR 明确依赖先合并 #29。
- **终审已完成**：独立最终 review 结论 **Approve，无 blocker / major / minor**（集成证据见下节）。#29/PR31 已合并，#29 base 已集成进 #30，仅剩创建 PR。

## 剩余工作

1. 创建 PR（#30 → 集成分支）。

## 依赖集成后证据（合并 PR31/#29 base 进 #30）

| 项 | 命令范围 | 结果 |
|---|---|---|
| 定向测试（reader / service / API） | 同上 reader/service/API 测试文件 | **40 passed** |
| 后端全量回归 | `backend` 全量测试套件 | **3483 passed, 3 skipped, 8 warnings，104.70s** |
| Lint | Ruff（F/E9 规则组） | **passed** |

独立最终 review 结论：**Approve，无 blocker / major / minor**。终审已闭合，状态进入「实现及依赖集成完成／待 PR」。

## scripts/tnt 趋势对照修正（2026-08-28，已验证）

PR #32 遗漏 `docs/TODO.md` 要求的 `scripts/tnt/` 单边趋势日「接飞刀」对照；已在 worktree 补齐（`daily_open_anchor_filter.py` 与对应 service 测试），新增确定性用例（直接构造 arms/layers，不触发 engine）：

- `test_trend_bucket_boundaries_are_inclusive_and_frozen`
- `test_tnt_contrast_status_rules_are_frozen`
- `test_tnt_contrast_down_adverse_range_improved_is_conditional_by_trend`
- `test_tnt_contrast_reads_oos_only_and_ignores_is_layers`
- `test_tnt_contrast_insufficient_sample_is_inconclusive_by_trend`
- `test_evaluate_payload_appends_tnt_open_anchor_contrast`

主会话最终验证证据（覆盖本修正及 P1 状态机、P2 非有限值参数化补充）：

| 项 | 命令范围 | 结果 |
|---|---|---|
| 定向测试 | service/API 相关测试文件 | **45 passed** |
| 后端全量回归 | backend 全量测试套件 | **3508 passed, 3 skipped, 8 warnings, 131.21s** |
| Lint | Ruff（F/E9 规则组） | **passed** |

独立最终 review 结论：**Approve，无 blocker / major / minor**。tnt 趋势对照修正至此验证闭合，状态为「修正完成待 PR」。

## PR #33 后续日型/波动率修正（2026-08-28，已验证，完成待 PR）

| 项 | 结果 |
|---|---|
| shape/volatility 定向测试（含边界、invalid OHLC、planned 门控） | 主会话 focused **59 passed** |
| Lint | Ruff（F/E9 规则组）**passed** |
| 后端全量回归 | 主会话 **3522 passed / 3 skipped / 8 warnings / 119.24s** |
| 独立最终 review | **Approve，无 blocker / major / minor** |

本波至此验证闭合，状态为「完成待 PR」。

## PR #32/#33 Review 评论收口（2026-08-28，已验证）

本波核验并修复 4 条合并后到达的未解决行级评论：PIT `is_st` 覆盖、T+1 `raw_low`
预检、涨停阻塞下的 `anchor_unavailable` ledger，以及 TNT 仓库内可审计来源。

| 项 | 结果 |
|---|---|
| 定向测试（service/API） | **67 passed** |
| Lint | Ruff（F/E9 规则组）**passed** |
| 后端全量回归 | **3530 passed / 3 skipped / 8 warnings / 145.43s** |
| 独立复核 | **Approved；四条评论均在实际执行路径闭环** |

本波状态为「Review 评论修复完成，待 PR」。

## 边界

- 本文件只记录主会话证据与状态，不包含新的验证执行。
- 红线不变：sealed-only、immutable generation pin、raw PIT bands、不写 `data/`、研究 payload 仅统计性结果。
