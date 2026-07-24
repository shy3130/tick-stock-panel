# 全球指数实时行情语义验收

- API 行为测试以 HK/US 实时 provider 样本验证字段、百分比单位、请求过滤和 `source=realtime`。
- UI 行为测试验证指数报价查询配置了不高于 10 秒的周期刷新，并保持 market/symbol 查询键隔离。
- 生产验收将 API 返回与同一时间窗口的 Longbridge HK/US 指数报价交叉核对，并确认数据时间持续推进。

## 2026-07-20 验收结果

- 后端 483 项、前端 41 项测试全部通过；TypeScript 与 Vite 生产构建通过。
- HK API 返回 3/3、US API 返回 4/4，顶层与逐行均为 `source=realtime`。
- API 的现价、昨收、涨跌额、百分数涨跌幅和时间戳与 ClickHouse/Longbridge 下层证据一致；缺失涨跌额可由现价减昨收确定性补算。
- 指数页报价查询按 market 和 symbols 隔离，并配置 6 秒可见页轮询。
