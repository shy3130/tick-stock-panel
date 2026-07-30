# 趋势监控突发异动高亮语义验收

状态：通过（实现、验证和 10.28 生产发布完成）

## 验收需求

- `REQ-DOW-MONITOR-SUDDEN-ANOMALY-HIGHLIGHT-001`

## 已批准语义

只检测相邻两次有效数据间的突然变化：

| 指标 | 触发阈值 |
| --- | --- |
| 涨跌幅 | 0.50 个百分点 |
| 1m 涨速 | 0.40 个百分点 |
| 1m 量速 | 1.00 倍 |
| 五档盘口压力 | 40 个百分点 |
| 距日高 | 0.50 个百分点 |
| 距日低 | 0.50 个百分点 |

触发后只高亮发生变化的具体数值，显示淡红背景、红色描边、红色文字和“异动”
标记，持续 10 秒。再次达到阈值时重新计时。

第一次有效值、缺失值、延迟值和数据恢复后的第一个有效值均不得触发。股票离开
当前页后必须清除其基线和高亮状态。

## 信号边界

异动高亮仅是客户端观察提示，不得生成、清除、翻转或升级持久化正式买卖信号，
不得修改后端分钟决策、通知、WebSocket 订阅或 3018/19912 职责。

## 测试先行证据

- 状态机 RED：行为测试首次执行因
  `suddenAnomalyHighlights` 模块不存在而失败；实现后 20 项通过。
- Hook RED：行为测试首次执行因 `useSuddenAnomalyHighlights` 模块不存在而失败；
  实现后 3 项通过。
- 列表 RED：行为测试首次找不到
  `anomaly-changePct-700.HK` 精确数值包装器；接入后列表 8 项通过。
- 帮助页 RED：新增验收首先找不到“突发异动高亮”章节和目录链接；实现后帮助页
  2 项通过。

## 验收结果

| 验收点 | 实现证据 | 可执行证据 | 结果 |
| --- | --- | --- | --- |
| 六项阈值及相邻有效值比较 | `suddenAnomalyHighlights.ts` | `suddenAnomalyHighlights.test.ts` 20 项 | 通过 |
| 首值、缺失、延迟、恢复和翻页重置 | `advanceSuddenAnomalyState` | 状态机边界测试 | 通过 |
| 10 秒过期及再次触发重计时 | `useSuddenAnomalyHighlights.ts` | Hook 3 项定时器测试 | 通过 |
| 只高亮具体数值并显示“异动” | `DowMonitorList.tsx` 的 `AnomalyMetric` | 列表精确包装器、样式和无障碍断言 | 通过 |
| 不改变正式信号及其时间 | 原有信号渲染路径保持不变 | 列表测试对 BUY 信号和时间的前后值断言 | 通过 |
| 帮助页完整说明阈值和边界 | `DowMonitorHelp.tsx` | 帮助页 2 项 | 通过 |

2026-07-30 完整聚焦测试共 5 个文件、50 项全部通过；生产构建
`pnpm build` 通过；三个相关规格契约文件共 6 项通过。

全量前端套件执行结果为 39 个文件中 38 个通过、176 项通过、2 项跳过、1 项失败。
唯一失败位于本需求未修改的 `src/pages/Screener.dow-strategy.test.tsx`，其既有断言
找不到“道氏趋势 · 多周期”；突发异动相关状态机、Hook、列表、页面和帮助测试均
在同一次全量执行中通过。

`python scripts/check_spec_compliance.py` 仍报告两个与本需求无关的仓库既有基线：

- 已过期例外 `EXC-COLLECTION-MONITOR-PREACCEPTANCE-DEPLOY-001`；
- `REQ-DOW-MONITOR-DETAIL-TOGGLE-LAYOUT-001` 的测试路径位于 `frontend/`。

本需求自身的权威规格、追踪、实现、测试和验收文件均通过专属契约。没有修改后端、
WebSocket 订阅、通知和正式信号决策。

## 生产发布证据

2026-07-30 发布到 `192.168.10.28:3018`：

- 源码提交：
  `6b9134a70cdcd052494608cb55dd87fadcf0ff41`
- 镜像：
  `tickflow-stock-panel-app:dow-monitor-sudden-anomaly-6b9134a-20260730-135855`
- 镜像 ID：
  `sha256:b616efacb815268b4bc25fc490e5c0989391cf5ce059a0f4b225108873a947f4`
- 隔离候选端口 `13018` 冷启动健康通过，容器 running、重启次数 0。
- 生产容器 running、重启次数 0，`/health` 返回版本 `0.1.86`。
- `/dow-monitor?market=hk` 和 `/dow-monitor/help?market=hk` 均返回 200；
  未登录 overview 返回 401，符合既有认证边界。
- 3018 只有一个监听；发布后 10 分钟日志扫描没有
  `ERROR|CRITICAL|Traceback`。
- 股票文件哈希保持
  `2d8da35aa9eb0da2faca894e72e1cd52e9518fad11a4844bc418962bfeb29ddb`，
  symbols API 哈希保持
  `25bb010891a99802a4d7dc7fe226d85eed9b8f9a928e7263d32a69abe0bb3b5c`。
- 备份：
  `/home/alwin/backups/dow-monitor-sudden-anomaly-predeploy-20260730-140329`
- 回滚容器：
  `TickFlow_Stock_Panel_pre_sudden_anomaly_20260730-140329`
  （上一版 P0 镜像，停止保留）。

生产 HTTP 静态文件与本地构建、隔离候选镜像三处哈希一致：

| 文件 | SHA-256 |
| --- | --- |
| `/app/static/index.html` | `c8cc2189230ec0886cce08f30ba2fa5e90ef36df2a7c698f763bc24e5ffc1b55` |
| `assets/DowMonitor-VCxyQaF_.js` | `23ee95ea25d8e7c2146811db60d0d9c9b9e190f9a3ae779aca37022f5c5f91cd` |
| `assets/DowMonitorHelp-DVyLq6bx.js` | `d928ee495ffe375b243fd7feb4d0cd6d6fba1c9b3a9707c429f4929e1a341022` |
