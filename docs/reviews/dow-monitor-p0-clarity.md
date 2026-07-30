# 趋势监控 P0 指标语义独立复核

状态：独立需求到证据复核通过（本地）；生产发布证据待补

## 复核方法

从每条权威需求反向检查实现、可执行行为测试和语义验收；不以构建成功、截图或快照替代指标公式与信号边界证明。

## 需求到证据

### REQ-DOW-MONITOR-P0-SEMANTICS-001

- 实现：
  `monitorListPresentation.ts` 将旧 `costDistancePct` 改为包含
  `vwap_price`、`vwap_distance_pct` 的 `vwap`，将旧
  `activeFunds.buyRatioPct` 改为 `capitalInflow.inflowRatioPct`；
  `DowMonitorList.tsx` 显示“VWAP”“资金流入”和逐项 15m/30m 周期确认。
- 可执行证据：
  纯函数测试使用手工期望值验证 VWAP、60/40 资金比例和两个周期身份；
  组件测试验证用户可见文案；帮助页测试验证避免持仓成本和逐笔主动买入误读。
- 复核结论：
  UI命名与实际数据源一致，没有把既有 0/2 改造成价格/资金两个新条件。

### REQ-DOW-MONITOR-P0-POSITION-RISK-001

- 实现：
  日内位置只读取非延迟实时 quote，并对 `(last-low)/(high-low)` 做
  0–100 限制；ATR同时保留绝对值和百分比，振幅/ATR使用日高低差除以
  完成 15m K线的绝对 ATR14。
- 可执行证据：
  手工数值 `101/102/95` 和 `7/2` 分别验证日内位置与振幅/ATR；
  缺失、延迟、零区间和样本不足均覆盖。
- 边界复核：
  `deriveMonitorRow` 的正式信号仍为
  `formal ?? (delayed ? null : warningSignal(item))`，新增观察值未进入信号分支；
  原“实时盘口变化不改变正式BUY信号”的测试继续通过。

### REQ-DOW-MONITOR-P0-FRESHNESS-001

- 实现：
  quote、depth、candlestick、analysis 各自读取源时间，分别计算非负秒龄；
  缺失保留 `null`，客户端延迟或 `ageSeconds > 90` 单独标记。
- 可执行证据：
  纯函数断言 `0s/5s/30s/30s` 和
  `330s delayed/--/--`；列表组件同时断言可访问名称。
- 边界复核：
  分字段延迟只改变字段颜色；没有写回 HTTP、WebSocket、分钟决策或通知。

## 反向遗漏检查

- 三条权威需求均有稳定ID、实现路径、`tests/` 下可执行契约、语义验收和本复核。
- 15m/30m 确认身份、VWAP原始值、资金流入口径、ATR绝对值和四类时间戳均从底层字段直接验证，没有用截图或构建成功替代语义证明。
- 没有修改后端、19912、3018端口、监控股票池、WebSocket订阅、通知或正式信号。
- 同时段RVOL、滚动盘口不平衡/近似OFI和撤单率属于后续 P1/P2，不在本次需求中，也没有用新名称伪装为已实现。

复核结论：三个 P0 需求在本地实现、行为测试和帮助文档间一致；当前剩余风险是尚未进行生产发布和生产页面登录态验收。
