# 道氏趋势监控四组指标语义验收

状态：实施中，尚未通过语义验收。

适用需求：

- `REQ-DOW-MONITOR-INDICATOR-GROUPS-LAYOUT-001`
- `REQ-DOW-MONITOR-LIVE-OBSERVATION-METRICS-001`
- `REQ-DOW-MONITOR-STABLE-DECISION-METRICS-001`
- `REQ-DOW-MONITOR-INDICATOR-SIGNAL-BOUNDARY-001`

实施完成后，按设计第 6 节进行语义验收：

- 缺少某一个子指标时，只将该子指标显示为 `--`；
- 不因一个实时字段缺失而隐藏整组稳定字段；
- 不用零值代替缺失；
- 不跨股票、跨交易日或跨周期沿用数据；
- 休市时稳定字段可继续显示最后一个有效后端快照，实时字段显示休市/不可用状态。
