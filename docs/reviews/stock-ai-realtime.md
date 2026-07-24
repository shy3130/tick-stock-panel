# 个股 AI 分析盘中实时数据独立需求审查

状态：2026-07-21 独立审查通过。

审查必须逐项核对 `REQ-STOCK-AI-REALTIME-001` 的实现、行为测试、接口元数据及生产验收，不得以构建成功或页面快照替代底层行情语义验证。

## 需求到证据核对

- 实时 OHLC、成交量和成交额合并：`backend/app/api/kline.py` 的既有实时蜡烛语义由新测试以 7 月 20 日日 K + 7 月 21 日盘中行情逐字段验证。
- AI 与关键价位共用数据：`backend/app/api/stock_analysis.py` 只加载一次合并后 DataFrame，分别传给价位计算和 `analyze_stock_stream`；测试直接核对两个上层出口。
- 资金状态使用盘中成交额：`backend/app/services/stock_analyzer.py` 对传入的合并后 DataFrame 调用 `_build_capital_metrics`，盘中行位于最后一行。
- 时效元数据：定向测试和生产接口均核对 `data_as_of`、`is_realtime`、`quote_timestamp`。
- 安全降级：空实时行情测试确认保留 7 月 20 日日 K 并返回 `is_realtime=false`。

## 验证记录

- 定向相关测试：8 项通过。
- 规格检查：`scripts/check_spec_compliance.py` 通过。
- 完整后端测试：496 项通过，1 项既有 `test_history_strategy_monitor_keeps_live_row_with_exclude_st_enabled` 失败；该测试独立复跑仍失败，涉及策略监控且不在本次变更文件与需求范围内，记录为非本需求回归。
- 生产容器镜像：`tickflow-stock-panel-app:stock-ai-realtime-20260721`；关键价位与 AI meta 对 `600519.SH` 均返回 2026-07-21 实时数据。
