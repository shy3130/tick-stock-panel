# 趋势监控快速首屏独立需求证据复核

状态：本地与生产需求到证据的独立复核均通过，所有发布门槛已关闭。

本文件不能以快照或单一 golden 代替语义证明。复核时逐项核对规格、生产实现、可执行测试、候选环境证据和回滚边界。

## 独立逐项复核

### REQ-DOW-MONITOR-FAST-BOOTSTRAP-001

- 实现把 symbols 查询与 list-overview 分离，页面用 symbols 计算当前页和订阅集合。
- 页面测试在 overview 为 loading 时验证订阅参数、实时价格和稳定字段加载态。
- 实时字段仍只进入既有 `deriveMonitorRow` overlay；没有产生正式信号的新增路径。
- 生产结论：通过。浏览器序列 `symbols finish 327 -> WebSocket create 328 ->
  subscribe frame 335 -> list-overview response 359`，订阅未等待稳定摘要。

### REQ-DOW-MONITOR-LIGHTWEIGHT-LIST-OVERVIEW-001

- 服务先 `list_states()` 一次并按语义股票身份/周期索引；测试禁止调用 `get_state()`。
- 裁剪边界与规格一致；前端派生等价测试不是快照，而是分别执行完整/裁剪输入并逐组比较业务结果。
- 真实状态中的 turning 仅保留完整 signals，bars 仅保留列表所需价格、成交量和均线字段；详情 pivots/lines/openingBoxes 不进入列表响应。
- legacy overview 也采用一次批量状态读取，未知旧调用方的响应结构保持不变。
- 生产结论：通过。真实美股 7 只为 751,529 bytes / 462.2 ms（只读候选），
  正式浏览器为 756,836 bytes / 1,068 ms；列表语义等价测试继续通过。

### REQ-DOW-MONITOR-NOTIFICATION-SUMMARY-001

- 新摘要路由排除 `snapshot_payload`、`prompt_text` 和 `evidence_text`，前端仍按 15 秒刷新。
- 文件签名未变化时测试证明不调用 `_load_notifications()`；追加和已读写入后同步更新签名。
- 旧完整通知和单条已读接口由 40 项 API 回归覆盖，正式信号选择逻辑未改。
- 生产结论：通过。100 条美股通知摘要只读候选为 35,938 bytes / 36.8 ms，
  正式浏览器为 36,066 bytes / 338 ms。

### REQ-DOW-MONITOR-STARTUP-PERFORMANCE-001

- 合成 20 股票与 100 通知载荷门槛通过；前端完整构建与 214 项测试通过。
- 本地证据不能替代 10.28 的真实 CPU、状态文件和网络条件。
- 生产结论：通过。旧正式基线约 56.2 s / 6.8 MB；新正式轻量 overview
  为 1.068 s / 756,836 bytes。WebSocket 完整快照冒烟为 820.0 ms，
  `/health`、静态块、日志和控制台均通过。

## 边界审查

- 未修改 19912 请求调度、分钟决策、AI Worker、正式通知生成或 ClickHouse 数据。
- 详情仍读取完整单周期接口；旧 overview/notifications 接口未删除。
- 回滚是只恢复上一 3018 镜像，不删除状态、通知、分钟结果或 AI 报告。
- 未发现权威规格冲突；HTTP 15 秒兜底仍存在，只是改为轻量 DTO。
- 3018 正式容器为 `302cb3682332...`、重启 0；AI Worker 容器 ID 和启动时间
  未变，19912 PID `3511290`、实时采集 PID `2891152` 未变。回滚只涉及 app 镜像。
