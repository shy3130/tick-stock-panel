# 全球指数实时行情契约

权威来源：用户要求港股、美股指数接入实时数据。本契约是既有《全路径市场作用域设计》中“HK 使用恒指系列、US 使用主要指数、不得以旧数据冒充实时”的聚焦澄清，两者无冲突。

## REQ-GLOBAL-INDEX-API-001（MUST）

`/api/intraday/indices` 对请求的 CN/HK/US 核心指数必须优先读取当前配置实时 provider 的当日最新快照，并返回 `source=realtime`。只有实时 provider 无对应数据时才允许返回明确标记为 `index_daily` 的历史兜底；不得把旧日线标记为实时。

响应中的 `last_price`、`prev_close`、`change_amount`、`change_pct` 和 `timestamp` 必须沿用实时数据契约，其中 `change_pct` 对外为百分数值（`2.36` 表示 `2.36%`）。请求包含 symbols 时不得混入未请求指数。

## REQ-GLOBAL-INDEX-UI-001（MUST）

指数页面必须按当前市场请求对应核心指数，并在页面保持可见时周期性重新获取实时指数报价；刷新周期不高于 10 秒。HK/US 不得显示 CN 指数报价或复用 CN 查询缓存。
