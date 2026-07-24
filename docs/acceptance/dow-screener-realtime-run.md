# 道氏选股实时执行验收

- 点击“执行选股”先 POST `/api/dow-strategy/runs`，不得直接读取榜单。
- 页面轮询 `/api/dow-strategy/runs/{runId}`，执行期间显示完成数、总数和当前股票。
- 只有任务状态变为 `complete` 后才读取 `/api/dow-strategy/pool` 展示本次结果。
- 上游扫描器使用 ClickHouse `lb_intraday_lines` 聚合分钟 K，不进行全市场长桥 SDK 行情扫描。

## 回归恢复证据（2026-07-24）

- `test_dow_strategy_proxy.py` 验证港股实时任务启动参数与上游代理路径。
- 道氏选股代理和现有监控接口联合回归共 `15` 项测试通过。
- 恢复仅重新接入实时任务代理，不改变 Longbridge 道氏趋势识别和筛选语义。
