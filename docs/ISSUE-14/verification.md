# 验证记录

## 代码范围

- `backend/app/services/volume_breakout.py`
- `backend/app/api/research.py`
- `backend/tests/services/test_volume_breakout.py`
- `backend/tests/api/test_volume_breakout_api.py`
- 本目录方案、Review 与最终设计文档

## 已执行命令

```text
PYTHONPATH=. /Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend/.venv/bin/python -m pytest tests/services/test_volume_breakout.py tests/api/test_volume_breakout_api.py -q
# 12 passed in 1.01s

PYTHONPATH=. /Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend/.venv/bin/python -m py_compile app/services/volume_breakout.py app/api/research.py
# 通过
```

## 观察到的结果

- 缺 generation-pinned reader、PIT eligible-universe 或版本化交易所 calendar 时，服务返回 `status=unavailable`、结构化 reason，事件/cluster/coverage/provenance 均为空；不使用 overlay、current universe 或日线 fallback。
- 完整 fake 三项能力可运行真实事件状态机与 OOS；不满足 production 能力门禁时不读取 bars、不制造事件。
- 请求模型拒绝未知字段、缺失日期及超过 1000 个标的；非法日期区间映射为 HTTP 400。
- 响应键通过交易语义禁令检查，不含买卖、仓位或执行建议字段。
- 生产仓库缺历史 PIT eligible-universe，因此没有宣称真实事件命中或 OOS 增量。

初始 focused tests 已覆盖契约；下节补充生产化状态与累计回归。

## 生产化波次（2026-08-27）

- 已实现严格早于 E 的 20 日 volume/amount P90、3–15 日整理、首次冻结且不重置、突破柱排除、上下严格突破、失败分层、1/5/10/20 日 forward、重叠 cluster、IS/OOS 与成本诊断。
- 真实 generation-pinned reader 和 versioned canonical calendar 已接通；完整 fake PIT universe 可运行事件路径。
- 生产仍缺 `effective_from/effective_to/available_at` 可审计的历史 universe 工件。当前 `instrument_info` 是现时快照，不能回填历史可用性，因此生产结果保持 unavailable，Issue 不关闭。
- 本波累计 focused/API 回归 `88 passed`；规范历史与指标管线回归 `90 passed`。

## 最终集成回归

- 六因子/API/provider/canonical/Agent/盘后管道累计：`351 passed, 7 warnings`。
- 改动 Python 文件 `ruff --select F,E9` 通过；前端 `pnpm exec tsc -b --pretty false` 通过。
