# 趋势监控快速首屏独立需求证据复核

状态：本地需求到证据复核通过；生产性能门槛仍需候选证据后最终关闭。

本文件不能以快照或单一 golden 代替语义证明。复核时逐项核对规格、生产实现、可执行测试、候选环境证据和回滚边界。

## 独立逐项复核

### REQ-DOW-MONITOR-FAST-BOOTSTRAP-001

- 实现把 symbols 查询与 list-overview 分离，页面用 symbols 计算当前页和订阅集合。
- 页面测试在 overview 为 loading 时验证订阅参数、实时价格和稳定字段加载态。
- 实时字段仍只进入既有 `deriveMonitorRow` overlay；没有产生正式信号的新增路径。
- 本地结论：通过。生产仍需浏览器网络时间线证明 subscribe 早于 list-overview 完成。

### REQ-DOW-MONITOR-LIGHTWEIGHT-LIST-OVERVIEW-001

- 服务先 `list_states()` 一次并按语义股票身份/周期索引；测试禁止调用 `get_state()`。
- 裁剪边界与规格一致；前端派生等价测试不是快照，而是分别执行完整/裁剪输入并逐组比较业务结果。
- legacy overview 也采用一次批量状态读取，未知旧调用方的响应结构保持不变。
- 本地结论：通过。20 股票夹具低于 1 MB；真实生产字节数待候选确认。

### REQ-DOW-MONITOR-NOTIFICATION-SUMMARY-001

- 新摘要路由排除 `snapshot_payload`、`prompt_text` 和 `evidence_text`，前端仍按 15 秒刷新。
- 文件签名未变化时测试证明不调用 `_load_notifications()`；追加和已读写入后同步更新签名。
- 旧完整通知和单条已读接口由 40 项 API 回归覆盖，正式信号选择逻辑未改。
- 本地结论：通过。

### REQ-DOW-MONITOR-STARTUP-PERFORMANCE-001

- 合成 20 股票与 100 通知载荷门槛通过；前端完整构建与 214 项测试通过。
- 本地证据不能替代 10.28 的真实 CPU、状态文件和网络条件。
- 本地结论：条件通过；候选 TTFB、首个 quote 和正式 3018 冒烟是发布硬门槛。

## 边界审查

- 未修改 19912 请求调度、分钟决策、AI Worker、正式通知生成或 ClickHouse 数据。
- 详情仍读取完整单周期接口；旧 overview/notifications 接口未删除。
- 回滚是只恢复上一 3018 镜像，不删除状态、通知、分钟结果或 AI 报告。
- 未发现权威规格冲突；HTTP 15 秒兜底仍存在，只是改为轻量 DTO。
