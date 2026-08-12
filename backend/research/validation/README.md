# Validation

冻结配置后的历史复验、组合验证和前向观察。

当前入口：

- `run_strategy_composition_wf.py`：策略 + 因子组合验证。
- `run_core_strategy_forward_watch_v1.py`：核心策略冻结观察。
- `run_structure_strategy_forward_watch_v1.py`：结构策略注册后观察。

模糊的区间复验、单笔诊断和旧集中仓位 walk-forward 已移入
`research/legacy/validation/`。新验证必须在文件名中写明对象和协议版本。
