# 验证记录

## 代码范围

- `backend/app/services/n_shape_golden_phoenix.py`
- `backend/app/api/research.py`
- `backend/tests/services/test_n_shape_golden_phoenix.py`
- 本目录方案、Review 与最终设计文档

## 已执行命令

```text
uv run --no-project python -m py_compile app/services/n_shape_golden_phoenix.py app/api/research.py
# 通过

/Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend/.venv/bin/python -m pytest tests/services/test_n_shape_golden_phoenix.py tests/services/test_short_pool.py tests/services/test_agent_research_tools.py tests/api/test_research_api.py -q
# 104 passed in 4.04s
```

## 观察到的结果

- 缺 generation-pinned reader 或 PIT 制度/ST provider 时，服务返回 `status=unavailable`，不使用 overlay 或现有 `signal_limit_up` 替代。
- raw 字段缺失、无效值、交易语义键均有确定性门禁测试。
- 现有短线池、Agent 工具和研究 API 相关完整测试未回归。
- 当前仓库没有真实 generation-pinned reader 与 PIT 历史制度/ST provider，因此没有宣称真实数据命中、OOS 增量或可执行成交；该能力缺口按设计显式 unavailable。

## PR #9 六条 review 修复

- evidence 字段改为 `price_range_rank_60d`，保留交易语义禁令；固定 evidence schema 的 `target` 键按结构字段处理。
- 结构保持检查覆盖首板后第 1–10 个市场日，并在确认柱自身 `raw_low < ref_low` 时删失。
- 缩量门禁与 `adjust_avg` 统一使用确认日前的调整柱，不计入确认柱。
- 首板后第 10 个预期市场日超出读取日历时返回 `post_window_truncated` 删失。
- 放量突破证据记录实际通过的均线（MA5 或 MA10），不再伪造 MA5 断言。
- generation-pinned reader 必须提供 64 位 manifest SHA-256；成功载荷记录 generation、manifest hash、factor code/version 与冻结参数。

本次定向验证：

```text
/Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend/.venv/bin/python -m pytest tests/services/test_n_shape_golden_phoenix.py -q
# 11 passed in 0.31s

./.venv/bin/python -m py_compile app/services/n_shape_golden_phoenix.py
# 通过（worktree 环境无 pytest，测试使用上方共享环境）
```

## 生产化波次（2026-08-27）

- 已新增 immutable canonical generation reader；真实 generation `20260817T132338-d20bb648` 冒烟读取 `600519.SH` 成功，manifest SHA-256 为 64 位。
- 事件研究层已补齐全部合格首板 baseline、IS/OOS 分层、1/5/10/20 日 forward、重叠 cluster、成本诊断、置信区间和 `accepted/rejected`。
- 当前仍无可证明的历史 PIT ST 名称/状态序列；`base_infos_history` 只有 2 个 snapshot day，`hsj_stock_type_change_records` 仅有 `MOVE_IN`，不能替代完整状态时间线。因此生产事件仍按设计 `unavailable`，Issue 不关闭。
- 本波累计 focused/API/Agent 回归分别为 `88 passed`、`110 passed`；规范历史与指标管线回归 `90 passed`。

## 最终集成回归

- 六因子/API/provider/canonical/Agent/盘后管道累计：`351 passed, 7 warnings`。
- 改动 Python 文件 `ruff --select F,E9` 通过；前端 `pnpm exec tsc -b --pretty false` 通过。

## PIT 生产数据接入（2026-08-27）

- composite reader 同时固定 canonical OHLCV generation 与 fstore markets generation；成功载荷分别记录两份 manifest SHA-256。
- markets facts 逐 symbol/date 提供历史 `name`、`is_st`、日期有效 `regime` 与 source exact `ztj`。缺任一字段时该日期进入 `limit_regime_unknown` 删失，不以当前名称或派生涨停信号替代。
- canonical `raw_open` 已进入必需列；首板 `raw_open == raw_high` 返回 `one_price_board`，不进入 baseline 或事件。
- 真实只读冒烟固定 canonical `20260827T054651-63f500a4` 与 markets `20260827T102014`，`600519.SH` 在 2022-03-04 至 2022-12-30 得到 201 个完整制度事实；评估返回 `status=ok`、`evaluated=1`、`censored=3`、`events=0`。这只证明生产数据链可运行，不宣称事件命中或因子准入。

定向测试：

```text
uv run --project /Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend \
  pytest backend/tests/data_providers/test_daily_market_research.py \
  backend/tests/services/test_n_shape_research_data.py \
  backend/tests/services/test_n_shape_golden_phoenix.py \
  backend/tests/api/test_research_factor_evaluate_api.py -q
# 30 passed in 4.71s
```

研究域回归：

```text
pytest daily_market_research + n_shape_research_data + n_shape_golden_phoenix \
  + research_sealed_data + research_registry + short_pool + agent_research_tools \
  + research_factor_evaluate_api + research_api + research_analysis -q
# 155 passed, 1 warning in 7.23s

ruff check --select F,E9 <本次 9 个 Python 文件>
# All checks passed
```

独立 coding review 首轮发现并修复两项 P1：

1. repository 不再缓存首次 `None` 或旧 generation；每个 API 请求重新固定双源 generation，并在 `finally` 关闭 facts DuckDB connection。
2. 首板后完整 10 日窗口任一 PIT fact 缺失时，两个事件变体均以 `limit_regime_unknown` 删失。

二次 review 确认两项 finding 均关闭，未发现新增 blocker/major。
