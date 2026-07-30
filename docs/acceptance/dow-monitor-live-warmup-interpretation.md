# 趋势监控历史预热期实时解读语义验收

- Requirement: `REQ-DOW-MONITOR-LIVE-WARMUP-INTERPRETATION-001`
- Status: accepted locally on 2026-07-30

验收必须证明：新鲜 WebSocket 行情不会被策略历史状态遮挡；历史不足时只输出实时观察，
不会产生正式买卖信号；真实行情延迟仍会正确降级。

## Evidence

- RED：新增行为测试后，历史预热场景的 `currentPrice` 实际为 `null`，重点解读实际为
  `ANOMALY_PENDING`，证明测试命中了旧行为。
- GREEN：
  `pnpm exec vitest run src/components/dow-monitor/interpretationMarketContext.test.ts src/components/dow-monitor/keyInterpretation.test.ts`
  共 14 项通过。
- 分层回归：
  `python -m pytest tests/spec_contracts/test_dow_monitor_live_warmup_interpretation_contract.py -q`
  会执行上下文、解释器、列表指标和趋势监控页面行为测试。
- 构建：`pnpm run build` 成功。
- 正式信号边界：`DowMonitor.test.tsx` 的
  `updates real-time price without changing the persisted signal` 继续通过。
