# ISSUE-18 验证记录

日期：2026-08-27

## 目标

验证单阳不破服务的语法、完整 focused test module 与变更范围；不提交。

## 命令与结果

在 worktree 的 `backend/` 目录执行：

- `uv run python -m py_compile ...`：未能执行，uv 创建环境时被仓库既有 `pyproject.toml` 的 `readme = "../README.md"` 路径校验拒绝（hatchling 要求 README 位于项目目录）。
- 使用现有 backend 虚拟环境等价执行 `python -m py_compile app/services/single_yang_no_break.py app/api/research.py`：通过。
- 使用现有 backend 虚拟环境执行 `python -m pytest tests/test_single_yang_no_break.py -q`：`5 passed`。
- `git status --short` / `git diff --check`：通过；状态仅列出本 issue 的 API、service、focused test 和 `docs/ISSUE-18/`。

## 范围门禁

实际范围检查未发现 `data/`、`short_pool`、Agent 或交易域文件变更；未创建 git commit。

## 生产化波次（2026-08-27）

- `compute_enriched` 现在在复权前原生保存 provider `open` 为 `raw_open`；`ENRICHED_STORAGE_COLS` 扩为 15 列，canonical manifest schema 升为 v2。禁止任何复权价反推。
- 单阳引擎已实现同 generation raw OHLC 门禁、固定 5 日窗口、T+5 确认、T+6 起评估、证据/删失、成本和 IS/OOS 分层；旧 generation 缺 `raw_open` 时测试锁定 unavailable。
- schema v2 generation `20260827T054651-63f500a4` 已原子发布：`17,220,261` 行、`5,679` 个有日线标的、`8,766` 个交易日、15 列且 `raw_open` 存在；固定 5 个源 generation，8 个只读 worker，旧 generation 在成功前始终由 `current.json` 服务。
- 本波 focused/API 回归 `88 passed`；规范历史与指标管线回归 `90 passed`。

## 真实路径收口

- `600519.SH`（2025-01-01 至 2026-08-14）真实单阳评估：`status=ok`、12 个事件、0 删失；IS 4、OOS 8。
- 研究 reader 的 manifest 字节 SHA-256 为 64 位，`has_raw_open=true`，没有读取本地 overlay。
- 首次 v1→v2 需要全量重发；后续盘后管道通过覆盖率校验后，从当前 v2 父 generation 克隆不可变旧分区，只复制新增本地 enriched 分区，完整 coverage scan 与父代 CAS 通过后原子发布。历史修订或 schema 变化才需要再次全量重发。

## 最终集成回归

- 独立 review 发现并修复 T+5 窗口缺交易日误判、worker snapshot pin 与 API workers 转发；二次 review 无阻塞 finding。
- 六因子/API/provider/canonical/Agent/盘后管道累计：`351 passed, 7 warnings`。
- 改动 Python 文件 `ruff --select F,E9` 通过；前端 `pnpm exec tsc -b --pretty false` 通过。

## 增量发布复核修正

- 修正提交 `d57bbf9` 已通过 PR #22 合并：incremental manifest 继承父 generation 的 `source_generations`，不会把发布时 current 指针误记为历史数据来源。
- 交易日连续性校验只 pin 必需的 `tdx` 与 `markets` generation，并把两者记录为 `calendar_source_generations`；不再要求五个数据源同时可用。
- `(parent_end, through_date]` 的预期交易日与本地 enriched 分区必须完全一致；缺失或多余日期均以 `calendar_partition_mismatch` fail-closed，并返回具体日期。
- 修正后定向验证：`tests/services/test_canonical_history.py` 为 `10 passed`；相关文件 `ruff --select F,E9` 通过。
