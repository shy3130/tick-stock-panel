# ISSUE-30 验证记录（verification）

关联：[Issue #30](https://github.com/wf2311/fm-workbench/issues/30) · [README](README.md) · [coding-review.md](coding-review.md) · [final-design.md](final-design.md)  
本文件记录主会话执行并核对的工程验证与真实 OOS 证据。最新日期：2026-08-29 · 初始基线：`7bf2982`。

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
- **终审已完成**：独立最终 review 结论 **Approve，无 blocker / major / minor**（集成证据见下节）。#29/PR31 已合并并集成；当时仅剩创建 #30 PR，后续也已完成。

## 当时剩余工作（已完成）

1. 创建并合并实现 PR：已完成；本节保留为历史时序。

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

本波已通过 PR #36 合并；PR #36 随后返回的两条新增评论由下节继续收口。

## PR #36 Review Follow-up（2026-08-28，已验证）

- T+1 `raw_low` 缺失、非有限或非正统一为整单 `limit_band_facts_incomplete`，且该门禁优先于 `invalid_open` 与 `horizon_data_gap` 等 candidate-level censor。
- TNT provenance 拆分为仓库内 `source` 证据摘录、真实 Obsidian `original_source` 与实现 `contract_source`；仓库摘录明确不把原笔记结果冒充本项目复验事实。

| 项 | 结果 |
|---|---|
| 定向测试（service/API，含门禁优先级组合） | **69 passed** |
| Lint | Ruff（F/E9 规则组）**passed** |
| 后端全量回归 | **3532 passed / 3 skipped / 8 warnings / 135.75s** |
| 独立复核 | **Approved；P1 优先级修正后二次复核通过** |

本波状态为「Follow-up 修复完成，待 PR」。

## 真实 OOS verdict（2026-08-29）

### Immutable provenance 与请求

- canonical generation：`20260829T002957-4b1bfcad`
- canonical manifest SHA-256：`0d5b5a457e7fa8c25bb047005b20cc6ca06ed19092f7ce20ba65f4604dfdd372`
- markets generation：`20260829T000704`
- markets manifest SHA-256：`a2a9d2b8208af33f4bcb66bcbe46a02ee836659c337deab4d0fd550ffead22a8`
- execution ledger：v3；日历口径 `pinned_market_days`
- 请求：`2025-02-25` 至 `2026-08-28`，OOS 起点 `2025-11-26`；确定性排序 10 标的；窗口 370 天上限。

### 整体 OOS

| 臂 | n_trades | stop_hit_rate | expectancy |
|---|---:|---:|---:|
| none | 55 | 0.345455 | -0.015449 |
| original | 9 | 0.111111 | -0.014426 |

原稿臂表面上止损率较低且 expectancy 略高，但仅 9 笔，未达到冻结的 `min_oos_trades=30`。因此 verdict 必须为 `inconclusive`，不得升级为 `validated`。

### TNT 预注册分层

| regime | none | original | status |
|---|---|---|---|
| `single_side_down` | 12 笔；stop 0.500000；expectancy -0.047312 | 0 笔 | `inconclusive` |
| `range` | 33 笔；stop 0.393939；expectancy -0.016162 | 7 笔；stop 0.142857；expectancy -0.016067 | `inconclusive` |

最终 `applicability=inconclusive_overall`；结果不进入默认策略池、Agent 排序或真实交易。机器可读摘要见 [oos-verdict.json](oos-verdict.json)。

## 边界

- 本文件只记录主会话证据与状态，不包含新的验证执行。
- 红线不变：sealed-only、immutable generation pin、raw PIT bands、不写 `data/`、研究 payload 仅统计性结果。
