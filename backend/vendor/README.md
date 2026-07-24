# 共享策略核心构建产物

`longbridge_stock-0.1.0-py3-none-any.whl` 来自本地仓库
`E:/my_project/longbridge-stock/.worktrees/dow-pattern-system-strategies`，源分支为
`codex/dow-pattern-system-strategies`，固定源提交为 `36d468c4d562912de103eeb783e188a657031a01`。
wheel 的 SHA-256 为
`dd86b11fbe4a4e152a69f44a404b52bb35b77330322fa13f823ba450c779d300`。

构建前已运行：

```powershell
python -m pytest tests/market_detectors tests/test_system_patterns_api.py -q
```

结果为 `158 passed`。扩展全仓运行结果为 `935 passed, 1 skipped`，另有 6 个与本次
形态核心无关的既有环境/期权同步失败；缺失的
`scripts/intraday_top3_health_report.py` 还会使对应既有测试无法收集。

重建命令：

```powershell
python -m pip wheel --no-deps `
  --wheel-dir E:\my_project\tickflow-stock-panel\backend\vendor `
  E:\my_project\longbridge-stock\.worktrees\dow-pattern-system-strategies
```

TickFlow 只安装该构建产物，不复制形态扫描器源代码。wheel 已验证同时包含
`longbridge_stock/system_patterns.py` 和既有结构突破扫描器。
