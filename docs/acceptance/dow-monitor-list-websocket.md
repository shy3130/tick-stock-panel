# 道氏趋势监控列表与 WebSocket 验收

状态：本地语义验收通过，待生产发布验收

## 语义验收场景

1. 分别选择 A 股、港股、美股，列表只出现对应市场股票，每页最多 20 只。
2. 在超过 20 只的受控数据集中切换页码，WebSocket 订阅集合只包含当前页已启用股票。
3. 连续推送 quote 与形成中 1 分钟 K 线，价格、涨跌幅、mini 趋势线末端更新，而通道、控制线距离、动量、量比、资金和买卖信号不变。
4. 返回新的完成分钟概览后，列表决策字段同步更新。
5. 后端正式通知存在时，列表展示对应买入、卖出或风险信号及其时间；失败突破不得升级为操作信号。
6. 数据延迟超过 90 秒或后端标记延迟时，列表明确显示延迟且不生成新的正式信号。
7. 点击任意行或“查看详情”，详细 K 线区域出现在列表下方，不出现模态框。
8. mini 趋势线只有一条折线，无背景、坐标轴、K 线或其他叠加线。

## 自动化证据

- `frontend/src/components/dow-monitor/monitorListPresentation.test.ts`
  - 完成 K 线过滤、15m/30m 通道、控制线回退、5m/15m 动量、资金质量、信号持久性、
    失败突破、延迟抑制、日内线和 20 只分页：`8 passed`。
- `frontend/src/components/dow-monitor/DowMonitorList.test.tsx`
  - 必要列、单折线、无背景、选中行、固定“查看详情”、延迟状态和分页：`3 passed`。
- `frontend/src/components/dow-monitor/DowMonitorDetailPanel.test.tsx`
  - 非 dialog 内嵌详情和周期/叠加层控制：`1 passed`。
- `frontend/src/pages/DowMonitor.test.tsx`
  - 三市场、20 行、当前页订阅、实时/决策边界、内嵌详情、市场切换和筛选：`7 passed`。
- `frontend/src/lib/realtimeMarketData.test.ts`
  - 订阅、重连、最新状态与每秒批量发布：`12 passed`。
- `tests/spec_contracts/test_dow_monitor_list_websocket_contract.py` 与
  `tests/spec_contracts/test_realtime_frontend_contract.py`：`2 passed`。
- `backend/tests/test_realtime_websocket.py`：`5 passed`。
- 除已知基线失败 `src/pages/Screener.dow-strategy.test.tsx` 外的前端套件：
  `35 files passed, 137 tests passed, 2 obsolete modal-integration tests skipped`。
- `pnpm --dir frontend build`：成功。

## 浏览器证据

在 1440×900 Chromium 中使用只读模拟 API 响应检查：

1. 市场入口仅有 A 股、港股、美股；
2. 表头包含全部十个约定字段；
3. mini 图 DOM 只有一个 `polyline`，没有背景矩形；
4. “查看详情”点击后行变为选中状态；
5. `1.HK 详细走势` region 位于列表和分页之后，页面没有 dialog；
6. 详情保留 5/15/30/60 分钟、日线、成交量、MACD、RSI、KDJ、BOLL、趋势线和头肩形态控制。

浏览器截图：`.playwright-cli/page-2026-07-29T05-57-21-674Z.png`（临时验收产物，
不纳入版本库）。

## 已知非本次阻断项

- 全量前端套件仍有基线已有的 Screener 文案测试失败：
  `Screener.dow-strategy.test.tsx` 查找“道氏趋势 · 多周期”失败；本次未修改 Screener。
- `scripts/check_spec_compliance.py` 仍报告两个基线问题：已过期的采集监控预验收例外，
  以及旧详情需求把前端测试路径登记在 `tests/` 目录之外。本次五条新需求不再新增该类错误。
- 尚未发布到 `192.168.10.28:3018`，生产 WebSocket、缓存和静态包验收仍待发布阶段执行。
