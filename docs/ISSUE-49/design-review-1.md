# Issue #49 设计复核一

- 冻结深度：`(swing_high_raw - pullback_low_raw) / (swing_high_raw - origin_low_raw)`。
- 公共 N0 不复用首板事件：先确认上涨段低点→高点，再确认回调；仅在后续收盘严格突破 swing high 时生成事件。
- A 为 `>0.50`，B 为 `[0.33, 0.50]`，C 为 `<0.33`；非法或越界深度 fail-closed。
- 回调 raw low 跌破 origin raw low 进入结构失败分支，不伪装为 C/A 档事件。
- 金凤凰仅作为 C 档交叉标签：回调均量同时不高于 swing-high 日量的 70% 与 origin 前 20 日均量的 90%；历史不足则标签 unavailable。
