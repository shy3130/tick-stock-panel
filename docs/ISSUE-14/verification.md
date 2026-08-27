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
- 即使注入满足方法形状的三项 stub 能力，事件状态机与 OOS 仍以固定未实现原因 fail-closed，不读取 bars、不制造事件。
- 请求模型拒绝未知字段、缺失日期及超过 1000 个标的；非法日期区间映射为 HTTP 400。
- 响应键通过交易语义禁令检查，不含买卖、仓位或执行建议字段。
- 当前仓库没有生产 generation-pinned reader、PIT 历史 eligible-universe、版本化逐标的交易日历；因此没有宣称真实事件命中、OOS 增量、forward 结果或可执行交易能力。

未运行项目级测试、lint 或 build；本记录仅覆盖上述 focused contract tests。
