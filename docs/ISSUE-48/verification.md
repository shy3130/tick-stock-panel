# Issue #48 验证

- 定向回归覆盖 escape-risk reader、catalog resolver、daily market PIT facts、S1-S10 检测、production、聚合与 API：`100 passed`。
- 完整后端：`3709 passed, 3 skipped, 8 warnings`（291.21s）；warnings 均来自既有 Polars sortedness/deprecation/performance 路径。
- changed-scope 编译与 Ruff `--select F,E9`：`All checks passed!`。
- 真实 catalog 路由复验：`tdx_trans/a/2025-08-28` 解析到 `engine-a-trans-archive/20260715T074351/tdx-trans-2025.duckdb`，不再误落到 2026-08 preliminary；新增“精确历史 immutable vs 无界 later preliminary”回归已通过。
- 真实 `600519.SH` 单日复验（2025-08-28）：240 根、逐笔成交量 `3,928,200` 股、amount `5,683,731,763`、累计 VWAP `1446.9048834071584`，无 reader unavailable。
- 真实 `600519.SH` 六日复验（2025-08-21/22/25/26/27/28）：6/6 symbol/day 完整；bar close 锚点为 09:31、10:30（index 59）、14:30（index 209）、15:00；2025-08-28 产生 S2-S7 非触发 evidence，S4/S10 前 5 日窗口完整，S10 因历史 `ltgb.available_at` 无法证明返回 `censor_pit_fact_missing`，没有使用当前股本替代。
- 边界回归额外锁定：S3 同分钟触板开板计数；S5 首次可成交翘板分钟与封死不可达；S10 可用事实但不触发时为 `qualified=false` 而非 PIT censor；盘中 raw execution price 以信号日复权比例转换后再计算 forward return。
- 第一轮独立 review：无 P0，1 个 P1（raw/adjusted 价格空间混用）；修复后第二轮独立复核结论“闭环”（confidence 0.92）。
- PR #52 GitHub Codex Review 状态为 Completed，4 条行级意见（2×P1、2×P2）均先由新增回归复现：修复前 `5 failed, 13 passed`，修复后专项 `18 passed`。新增边界覆盖 canonical bar close 时间、ST 5% 跌停制度、S7 平/降开盘窗和 S3 连续触价/重新封板后的开板 episode。
- PIT 补漏回归先以 NULL historical name 复现错误的 `main_10` 跌停事实，修复后 `limit_band_facts`/`escape_risk_facts` 均返回空；daily-market reader 专项 `6 passed`。
- 输出只有研究事实、provenance、coverage、censor 和统计；不含交易方向、订单或自动执行动作，`promoted=false`。
