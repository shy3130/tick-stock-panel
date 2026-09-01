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

## TODO 研究扩展（2026-08-31）

- 新增十字星、筹码峰、周线旗杆、逃生窗口与指数 reader 专项：`65 passed`；既有 hold-firm/N 字/API 研究回归：`116 passed`。
- 最终完整后端：`3774 passed, 3 skipped, 8 warnings`（157.25s）；warnings 仍仅来自既有 Polars sortedness/deprecation/performance 路径。
- 新增模块完整 Ruff 规则与全部变更 `F,E9` 检查均为 `All checks passed!`；`git diff --check` 无输出。
- 真实 API 复验：十字星 `600519.SH` 返回 644 个父事件日，D1 34 个合格事件且因 OOS 四桶/bootstrap 门槛保持 `unavailable`；周线旗杆返回 192 个完整周、20 个旗杆、3 个合格事件；筹码峰因 `2022-12-21` PIT 换手事实不可证明而 fail-closed 为 `unavailable_pit_turnover_provenance`。
- 真实逃生窗口全周期复算（2007-01-01 至 2026-08-28）：4,797 个交易日、全 A 14,597,621 行、四条市场腿各 24 个主单元、1,056 个敏感性单元、763 个严格 censor；全 A 覆盖 2007-2026，三指数 pinned 日线仅覆盖 2013-2026，故保守保持 `[~]`。
- 独立 reviewer 首轮 6 个 P1 均由回归复现并修复；第二轮复核结论为“无 findings”。

## 可审计 OOS 退出裁决扩展（2026-08-31）

- `POST /api/research/factors/escape-risk/evaluate` 现强制调用方显式提交 `oos_start`，并拒绝不在 `[start, end]` 内的边界；production 响应回显该冻结边界。
- S1-S10 的每个合格事件按同一持仓样本配对比较立即退出、无信号持有、MA20 退出和 ATR 吊灯退出；以终值最高的基线为 strongest baseline，方向统一为 `signal_exit - baseline`，同时披露卖飞与规避回撤。
- OOS 裁决使用 symbol-cluster paired bootstrap；标的簇或有效复算不足时保持 `unavailable`，不再误标为 `rejected`。MA20/ATR 在倒数第二根触发时固定按下一根开盘执行，避免最后一根收盘偷看。
- 独立 reviewer 首轮指出 5 个有效边界问题（另含 MACD/单阳共用裁决层），均新增回归后修复；二次只读复核结论为 `no findings`。
- 本轮研究定向套件最终为 `78 passed`；实现后完整后端为 `3803 passed, 3 skipped, 8 warnings`（170.76s）。收敛 API 无关格式差异后再次运行定向套件仍为 `78 passed`，changed-scope Ruff 与 `git diff --check` 均通过。
- 真实 `600519.SH` 请求（2025-07-01 至 2025-08-29，`oos_start=2025-07-15`）返回 `status=ok` 且 intraday reader 可用；S8/S7 因单标的样本不足保持 `unavailable`，S10 因 PIT 事实缺失保持严格 censor，没有生成伪结论。
