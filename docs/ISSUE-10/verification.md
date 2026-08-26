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

## 未验证 / 明确边界

- 当前实现对完整 reader 仍以 `direction_evaluator_pending` fail-closed；reader 契约深层链路不可达，事件主路径、真实 15m/5m 方向标注、OOS 结果和执行可达性均未实现。
- 生产环境没有 immutable minute reader，因此没有真实分钟研究结论，不将本 Issue 标记为 TODO 完成。
