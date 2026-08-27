# 验证记录

## 环境路径

- `app.__file__`: `/Users/wf2311/Projects/wf2311/fm/tickflow-issue-10-mtf/backend/app/__init__.py`
- `mtf_direction_15m5m.__file__`: `/Users/wf2311/Projects/wf2311/fm/tickflow-issue-10-mtf/backend/app/services/mtf_direction_15m5m.py`

## 已执行命令

```text
uv run --no-project python -m py_compile app/services/mtf_direction_15m5m.py app/api/research.py
# 通过

/Users/wf2311/Projects/wf2311/fm/tickflow-stock-panel/backend/.venv/bin/python -m pytest tests/services/test_mtf_direction_15m5m.py tests/api/test_research_api.py -q
# 22 passed in 2.03s
```

## 已验证

- 无 reader 时稳定返回 `status=unavailable`。
- 请求 `extra=forbid`、日期窗口、canonical symbol 校验和重复 symbol 去重均有测试。
- 交易语义 key 禁令有测试。
- 现有研究 API 完整测试未回归。

## 初始波次边界（已由后续生产化波次替代）

初始 PR 只验证 fail-closed 契约；下节记录当前实现与仍然有效的物理数据缺口。

## 生产化波次（2026-08-27）

- 完整 `ImmutableMinuteReader` 注入后，方向引擎已不再返回 `direction_evaluator_pending`；真实 OHLCV、session、timestamp、cutoff 任一不满足仍 fail-closed。
- 引擎实现完整 1m→5m→15m 聚合、确认延迟、ATR 归一化斜率、方向质量、5m 同/反向确认、未来 1/2 根 15m 诊断、MFE/MAE、成本与 IS/OOS/基准分层。
- 真实表探针：`market_minutes` 仅有 `dataset/market/code/trade_date/minute_index/time/price/volume/amount`，没有原生 OHLC；`amount` 全空。禁止把 price 序列重建为真实 OHLC，因此生产 reader 仍不可注册，Issue 不关闭。
- 本波相关累计 focused/API 回归 `88 passed`。

## 最终集成回归

- 六因子/API/provider/canonical/Agent/盘后管道累计：`351 passed, 7 warnings`。
- 改动 Python 文件 `ruff --select F,E9` 通过；前端 `pnpm exec tsc -b --pretty false` 通过。
