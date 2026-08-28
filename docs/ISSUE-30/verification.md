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
- **终审（final review）待基线集成后进行**：先合并 #29，再把 #29 base 集成进 #30，之后由主会话组织最终评审。

## 剩余工作

1. 合并 #29（reader/publisher 单一来源）。
2. 将 #29 base 集成进 #30 分支，重跑定向 + 全量 + Ruff。
3. 终审通过后创建 PR（#30 → 集成分支）。

## 边界

- 本文件只记录主会话证据与状态，不包含新的验证执行。
- 红线不变：sealed-only、immutable generation pin、raw PIT bands、不写 `data/`、研究 payload 仅统计性结果。
