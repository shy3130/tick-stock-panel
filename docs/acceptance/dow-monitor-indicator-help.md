# 趋势监控指标帮助页语义验收

日期：2026-07-29

状态：PASS

## 验收范围

- `REQ-DOW-MONITOR-HELP-NAVIGATION-001`
- `REQ-DOW-MONITOR-HELP-CONTENT-001`
- `REQ-DOW-MONITOR-HELP-ACCESSIBILITY-001`

## 可执行证据

```powershell
pnpm --dir frontend exec vitest run `
  src/pages/DowMonitorHelp.test.tsx `
  src/pages/DowMonitor.test.tsx `
  src/pages/dow-monitor-route.test.tsx `
  src/components/dow-monitor/DowMonitorList.test.tsx `
  src/components/dow-monitor/monitorListPresentation.test.ts `
  src/lib/realtimeMarketData.test.ts
# 6 files, 43 passed

python -m pytest tests/spec_contracts/test_dow_monitor_list_websocket_contract.py -q
# 2 passed

pnpm --dir frontend build
# PASS
```

`python scripts/check_spec_compliance.py` 仅报告两个变更前已存在的基线问题：

- 已过期的 `EXC-COLLECTION-MONITOR-PREACCEPTANCE-DEPLOY-001`；
- `REQ-DOW-MONITOR-DETAIL-TOGGLE-LAYOUT-001` 的旧前端测试路径不在 `tests/` 下。

本次三个帮助页需求均已登记到 `docs/spec-index.yaml` 与
`docs/traceability.yaml`，并由上述契约测试检查。

## 导航与市场保持

- 生产趋势监控顶部只有一个“指标说明”入口。
- 港股入口为 `/dow-monitor/help?market=hk`，返回为 `/dow-monitor?market=hk`。
- A 股入口为 `/dow-monitor/help?market=cn`。
- 美股入口为 `/dow-monitor/help?market=us`。
- 缺失或非法市场由 `normalizeMarket()` 回退为 `hk`，行为测试覆盖。
- 全局导航没有新增帮助页一级入口。

## 内容与信号边界

生产页包含 7 个语义区块：

1. 快速决策路径；
2. 趋势 / 位置；
3. 动能 / 涨速；
4. 量价 / 资金；
5. 突破 / 风险；
6. 典型组合场景；
7. 指标速查表。

逐项核对的 14 个指标为：通道、控制、成本、1m、5m、15m、量比、量速、
主买、五档、高、低、ATR14、确认。其中“5m / 15m”在速查表合并为一行，
正文分别解释。

页面明确说明：

- “实时”只用于盘中观察；
- “稳”来自完成 K 线或后端决策；
- `--` 表示缺失或不满足稳定条件，不表示 0；
- 距离、ATR、盘口、实时动能和组合场景均不能生成、翻转或升级正式信号；
- 正式买卖方向和发生时间仍以后端持久化信号为准。

## 浏览器语义与响应式证据

使用已登录 Chrome 对生产 `3018` 页面进行 cache-busting 验收：

- 标题“趋势监控指标说明”可见，7 个 `section[id]` 与目录锚点一一对应；
- “实时”“稳”状态文字可见，返回链接保持当前市场；
- 桌面视口 `documentElement.scrollWidth == clientWidth`；
- 390×844 视口下页面为 `390 == 390`，没有文档级横向溢出；
- 移动目录为局部滚动：`795 > 365`；
- 指标速查表为局部滚动：`900 > 331`；
- 控制台 error 数为 `0`。

CDP 网络记录显示帮助页只加载静态帮助分包
`assets/DowMonitorHelp-hW06EmEj.js`；没有 `/api/dow-monitor/*` 请求，也没有
`Network.webSocketCreated` 事件。应用壳自身的设置、能力、指数与实时流请求仍按
全局布局运行，不属于帮助页新增行情依赖。

## 生产发布与回滚

- 源码提交：`e0b636d`。
- 正式镜像：
  `tickflow-stock-panel-app:dow-monitor-indicator-help-e0b636d-20260729-203440`。
- 镜像 ID：
  `sha256:6c7af798b768fd99649da9b9e229e21a284c9fa0dd5b42fe4beaae7517fdbb2d`。
- 回滚镜像：
  `tickflow-stock-panel-app:dow-monitor-stable-fallback-041a384-20260729-200151`。
- 发布前备份：
  `/home/alwin/backups/dow-monitor-indicator-help-predeploy-20260729-203440`。
- 容器状态：`running`，重启次数 `0`。
- `/health`：
  `{"status":"ok","version":"0.1.86","mode":"none"}`。
- 发布窗口日志没有 `ERROR`、`CRITICAL` 或 `Traceback`。
- `dow_monitor_symbols.json` 发布前后 SHA-256 均为
  `1d5955494b4a74d8ae32bd550e4f744e09c4cab80c85f190acd01f3717bef59e`。

静态资产在本地、容器与生产 HTTP 三处 SHA-256 一致：

| 文件 | SHA-256 |
| --- | --- |
| `/app/static/index.html` | `536c6412046d434cec95ee1c0219c4981afef0d0fc66a1b179dbce0a2615e6f6` |
| `assets/index-VK-atCLA.js` | `0c70709cdbe3ac251ac71a3a6cc4236c0fd2f1ded377d8975d1b3710995bbf3f` |
| `assets/DowMonitor-D9iy2_6T.js` | `2faa0b91086b23201a5657e698b1351ca1fe36e142d67d71907d3f4991b69508` |
| `assets/DowMonitorHelp-hW06EmEj.js` | `a2c2828bcdb4e450558543bc9228ca4e3f5bd1241f4b4664959e9c0c48e33d29` |

以上证据同时覆盖实现、可执行测试、真实浏览器语义和生产运行态，本次验收结论为
PASS。
