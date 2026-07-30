# 趋势监控 P0 指标语义验收

状态：本地语义验收通过；生产 10.28 发布与运行态验收通过

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

本次仅替换 10.28 的 3018 静态前端层；19912、后端代码、WebSocket
采集、监控股票池、完成分钟决策和正式信号均未修改。

### 生产发布证据

- 源码提交：
  `45ec4b0a36525569df0fac9524f70de684b70a84`。
- 生产镜像：
  `tickflow-stock-panel-app:dow-monitor-p0-prebuilt-45ec4b0-20260730-100305`；
  镜像 ID：
  `sha256:8df2351ad5d7fd569d6f2c24e65053ed6496a0fd470d7f23f3c9c7defd58d11a`。
- 镜像 revision 标签与源码提交一致；release 标签为
  `dow-monitor-p0-clarity`。
- 生产容器 `TickFlow_Stock_Panel` 为 `running`，`RestartCount=0`；
  `/health` 返回
  `{"status":"ok","version":"0.1.86","mode":"none"}`。
- 发布前备份：
  `/home/alwin/backups/dow-monitor-p0-predeploy-20260730-102500`；
  回滚容器：
  `TickFlow_Stock_Panel_pre_p0_20260730-102500`，保持停止状态。
- `dow_monitor_symbols.json` 发布前后 SHA-256 均为
  `2d8da35aa9eb0da2faca894e72e1cd52e9518fad11a4844bc418962bfeb29ddb`；
  `/api/dow-monitor/symbols` 发布前后响应逐字节一致，SHA-256 为
  `25bb010891a99802a4d7dc7fe226d85eed9b8f9a928e7263d32a69abe0bb3b5c`。

生产容器中的静态文件与本地验收构建逐字节一致：

| 文件 | SHA-256 |
| --- | --- |
| `/app/static/index.html` | `07f6b15274806a00cf82fadd35d2c15ddaed9189d7a90eba38f4d9290b701c83` |
| `assets/index-VN-aagq2.js` | `c607087a56931247190b6ea60204b67653d0f8f753d16b3d3c620754e1cbe1b5` |
| `assets/DowMonitor-iI7jNzSf.js` | `f19c03e2dbbd6640e696d8f7873ad41e04d62136c2fe263073cd948da9bf8a12` |
| `assets/DowMonitorHelp-9sYAbmUR.js` | `bb29ebba43f1f02f69444fb062845e4049c9024ee841faad2b81ec27e2aafb5c` |

生产日志确认应用启动完成、WebSocket 重新连接，已登录客户端的
`/api/dow-monitor/overview?market=hk` 返回 200。受控内置浏览器没有生产
登录态，只验证到登录边界，没有输入或读取访问密码；生产语义证据由逐字节一致
的静态包、组件行为测试和底层公式测试共同提供。
