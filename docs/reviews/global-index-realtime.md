# 全球指数实时行情独立审查

状态：2026-07-20 已通过。

审查顺序：先确认 Longbridge → ClickHouse 当日指数快照语义，再确认 TickFlow API 字段与来源标记，最后确认 UI 自动刷新。页面显示、SSE 或前端测试不得替代下层数据验收。

独立复核结论：REQ-GLOBAL-INDEX-API-001 的 provider 优先、字段单位、请求过滤、实时/日线来源标记均有行为测试和生产响应证据；REQ-GLOBAL-INDEX-UI-001 的市场查询键和 6 秒轮询有可执行测试与生产构建证据。下层 ClickHouse 已先于 API/UI 验收通过，无未决权威冲突。
