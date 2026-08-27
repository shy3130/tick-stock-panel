# 验证记录

```text
uv run --no-project python -m py_compile app/services/weak_to_strong.py app/api/research.py
# 通过

/Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend/.venv/bin/python -m pytest tests/services/test_weak_to_strong.py tests/api/test_research_api.py -q
# 21 passed in 2.06s
```

初始 PR 已验证缺 reader 的结构化 unavailable、请求 schema、重复 symbol 与交易词禁令；生产化波次继续覆盖事件/OOS 主路径和真实数据能力探针。

## 生产化波次（2026-08-27）

- 全能力 reader 已可产出 `one_word_limit/sealed_limit/broken_resealed/broken_not_resealed/gap_up_no_touch/no_gap_up/bar_touched` 等可审计分类，不再返回 `event_path_not_implemented`。
- PIT、停牌、时间线、逐笔排序、竞价、盘口可达性、无盘口时 `bar_touched` 降级、前向删失、成本与 IS/OOS 摘要均有 focused tests。
- 生产数据硬缺口仍存在：历史 PIT 盘口没有来源；`base_infos_history` 仅 2 个 snapshot day，无法支撑完整 ST/流通股本时间线。因此生产 registry 保持空，Issue 不关闭。
- 本波累计 focused/API 回归 `88 passed`。

## 最终集成回归

- 六因子/API/provider/canonical/Agent/盘后管道累计：`351 passed, 7 warnings`。
- 改动 Python 文件 `ruff --select F,E9` 通过；前端 `pnpm exec tsc -b --pretty false` 通过。
