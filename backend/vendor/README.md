# 共享策略核心构建产物

`longbridge_stock-0.1.0-py3-none-any.whl` 来自本地仓库
`E:/my_project/longbridge-stock/.worktrees/structure-breakout-scanner`，源分支为
`codex/structure-breakout-scanner`，固定源提交为 `24b24b8`。
wheel 的 SHA-256 为
`62fc628f83809a57953f269765a1e0e5adcb64bdb7d24610a0524ed32053cc78`。

构建前已运行：

```powershell
python -m pytest tests/test_structure_breakout_scanner.py tests/test_scan_structure_breakouts.py -q
```

结果为 `20 passed`。

重建命令：

```powershell
python -m pip wheel --no-deps `
  --wheel-dir E:\my_project\tickflow-stock-panel\backend\vendor `
  E:\my_project\longbridge-stock\.worktrees\structure-breakout-scanner
```

TickFlow 只安装该构建产物，不复制结构突破扫描器源代码。
