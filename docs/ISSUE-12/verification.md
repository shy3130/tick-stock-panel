# 验证记录

```text
uv run --no-project python -m py_compile app/services/weak_to_strong.py app/api/research.py
# 通过

/Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend/.venv/bin/python -m pytest tests/services/test_weak_to_strong.py tests/api/test_research_api.py -q
# 21 passed in 2.06s
```

初始 PR 已验证缺 reader 的结构化 unavailable、请求 schema、重复 symbol 与交易词禁令；生产化波次继续覆盖事件/OOS 主路径和真实数据能力探针。

## 生产化波次（2026-08-27，已验证）

- 已实现 production composite reader/API seam：minimum/full capabilities、component manifest/composite SHA-256、pinned canonical/markets/#10 sparse minute/signal-year callauction。
- PIT 记录固定 markets generation `created_at` 为 `available_at`，`effective_at` 与 `available_at` 均按 09:25 Asia/Shanghai 双门禁；事件计算优先 exact `ztj`。
- ticks/books/float 首版明确 unavailable；触板、封板和一字板分支在相关证据缺失时只返回 `bar_touched`/删失，不伪造 sealed 分类。production reader 成功/异常均由 API finally 精确级联关闭。
- 定向合同/API 测试：`34 passed in 4.43s`。
- 改动 Python 文件 `ruff check --select F,E9`：通过。
- 真实 production reader smoke：reader 构造成功，capabilities 为 minimum + callauction，composite manifest 固定 `canonical/markets/ordered_trans/callauction` 四组件；`2026-08-26` 历史 PIT 与 `2026-08-27` 同日 PIT 均因当前 generation publication 晚于 09:25 正确返回 `None`，未产生 sealed/one-word/resealed 分类。
- 独立二次 Review：最初发现裸 code 路由、09:25 时间校验与 signal-year logical pin 缺口；修复后复核无 blocker/major。

## 最终集成回归

- 当前分支后端全量：`3436 passed, 3 skipped, 8 warnings in 234.95s`。
- 改动 Python 文件 `ruff --select F,E9` 通过；前端 `pnpm exec tsc -b --pretty false` 通过。
