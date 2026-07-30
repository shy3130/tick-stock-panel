# 趋势监控历史预热期实时解读

## Authority

- Status: authoritative
- Approved by: user approval on 2026-07-30
- Requirement: `REQ-DOW-MONITOR-LIVE-WARMUP-INTERPRETATION-001`

## REQ-DOW-MONITOR-LIVE-WARMUP-INTERPRETATION-001

当 `/dow-monitor` 中股票的策略历史、资金或完成周期尚未就绪，但当前页
WebSocket Quote、Depth 或一分钟 Candlestick 仍然新鲜时，重点解读 MUST：

- 继续显示当前价格、日高日低、1 分钟涨速、量速和五档盘口中实际可用的实时证据；
- 明确区分“实时行情正常”和“策略历史预热中”，不得笼统显示为“关键数据延迟”；
- 输出“实时观察”或“异动待确认”，并用可见数值说明已经发生的价格、量能或盘口变化；
- 明确列出仍缺少的完成 5m/15m、资金或结构确认；
- 不得生成、清除、翻转或升级正式买卖信号。

只有 Quote/Candlestick 实时链路本身过期或当前价格不可用时，才可以把实时重点解读
降级为“关键数据延迟”。历史预热完成后，场景解释器恢复使用已有稳定结构与完成
K 线规则。

## Acceptance

1. `HISTORY_INCOMPLETE` 或等价策略状态不会遮挡新鲜 WebSocket 价格。
2. 历史不足但实时数据新鲜时，重点解读包含至少一个当前数值和“待完成周期确认”边界。
3. 同一输入下正式买卖信号与本需求实施前保持一致。
4. Quote 或 Candlestick 实际延迟时仍显示“关键数据延迟”。
