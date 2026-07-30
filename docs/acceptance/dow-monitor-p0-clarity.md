# 趋势监控 P0 指标语义验收

状态：本地语义验收通过；生产发布待执行

## 验收范围

- REQ-DOW-MONITOR-P0-SEMANTICS-001
- REQ-DOW-MONITOR-P0-POSITION-RISK-001
- REQ-DOW-MONITOR-P0-FRESHNESS-001

## 语义验收清单

1. 列表不再把日内 VWAP 偏离称为“成本位置”。
2. 列表不再把累计资金流入占比称为“主买”。
3. 周期确认分别展示 15m、30m，并保持后端分钟决策的周期语义。
4. 日内位置和振幅/ATR按权威公式计算，边界和缺失数据不伪造为零。
5. 行情、盘口、1m K线、分析各自展示数据年龄，延迟字段独立弱化。
6. 实时观察字段变化不改变持久化正式信号。

## 执行证据

### RED

首次执行三个直接行为测试文件时得到 8 项预期失败：

- `trendPosition.vwap`、`capitalInflow`、`intradayPositionPct`、
  `dayRangeAtrRatio` 和分字段 `freshness` 尚不存在；
- 列表仍显示“成本”“主买”和聚合“确认 N/2”；
- 帮助页仍使用旧指标口径。

失败原因均为目标行为尚未实现，不是测试语法或环境错误。

### GREEN

命令：

```powershell
pnpm exec vitest run `
  src/components/dow-monitor/monitorListPresentation.test.ts `
  src/components/dow-monitor/DowMonitorList.test.tsx `
  src/pages/DowMonitorHelp.test.tsx
```

结果：`3 passed`、`25 passed`。

行为断言包括：

- VWAP 价格 `10.48` 和偏离 `0.19%` 分开保留；
- 完整资金流入/流出 `60/40` 得到资金流入占比 `60%`；
- 15m、30m分别输出确认状态；
- 行情 `101`、日高 `102`、日低 `95` 得到日内位置
  `6/7 * 100`；
- 日高低差 `7`、绝对 ATR14 `2` 得到振幅/ATR `3.5`；
- 行情、盘口、1m K线、分析分别得到 `0s/5s/30s/30s`；
- 字段缺失、行情延迟和 `high == low` 返回缺失而不是零；
- 实时盘口变化不改变已持久化 BUY 正式信号。

### 契约与构建

- `python -m pytest tests/spec_contracts/test_dow_monitor_p0_clarity_contract.py tests/spec_contracts/test_dow_monitor_list_websocket_contract.py -q`
  结果：`4 passed`。
- `pnpm build` 成功；生成列表分包
  `assets/DowMonitor-iI7jNzSf.js` 和帮助页分包
  `assets/DowMonitorHelp-9sYAbmUR.js`。
- 全量前端测试结果：`151 passed`、`2 skipped`、`1 failed`。
  唯一失败为既有且可独立复现的
  `Screener.dow-strategy.test.tsx`，其页面不再渲染测试期待的
  “道氏趋势 · 多周期”，与本次趋势监控列表文件无关。
- `pnpm lint` 无法执行，因为仓库当前依赖未安装 `eslint` 可执行文件。
- 规格检查只剩两个本次修改前已存在的问题：过期的 collection-monitor
  例外，以及旧详情需求把测试路径登记在 `frontend/src`；本次三个新需求没有合规错误。

### 生产边界

本次没有发布到 10.28。生产 3018 仍为上一版本，待用户另行要求正式发布。
