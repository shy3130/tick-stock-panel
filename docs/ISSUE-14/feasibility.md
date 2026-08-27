# 可行性

有条件可行。sealed canonical 日线具备 raw OHLCV、成交额，`patterns.py` 有原型但无事件状态机/双缩量/方向评估。必须新建独立契约；数据、PIT 时点或窗口不完整时 fail-closed。现有短线池与 Agent 不改。