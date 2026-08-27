# 可行性评估

结论：**有条件可行，不能直接进入默认短线池**。

现有 `MarketDataProvider.get_minute(..., freq)` 与 `FQuantProvider` 已支持 1m/5m 聚合，15m 可从同一 sealed 分钟输入聚合；`MinuteExecutionData` 和 `fill_reachability` 可复用分钟组织/可达性诊断。但 QueryService 没有分钟谓词面，回测 minute 路径只负责撮合，不负责线段方向事件；全市场分钟扫描受 catalog 覆盖、标的/窗口资源上限约束。因此需要独立研究服务，缺分钟连续性、catalog generation、聚合时区或确认时点证据即 `unavailable`。

外部视频/看盘方法只作为规则来源，不作为统计证据。实现必须先冻结参数，再执行基线、有限训练调整、OOS 复核；无稳定增量则 `rejected`。
