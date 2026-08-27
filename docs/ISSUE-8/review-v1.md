# 方案 v1 Review

独立 Review 结论：**不通过，必须修订后再编码。**

## P0/P1

1. `get_enriched_range` 会合并 local overlay，不能仅凭 `build_data_snapshot` 的 generation/hash 证明本次读取固定 generation；必须增加 generation-pinned、排除 overlay 的读取前置能力，否则返回 unavailable。
2. `signal_limit_up` 的制度/ST 判定不具备完整 PIT 历史语义；首板必须定义为日期有效制度和 PIT ST 共同证明的涨停，且此前完整 60 个市场交易日无价格涨停，否则删失/unavailable。
3. enriched 没有 `raw_open`；不得把前复权 open/close 推导实体低点再与 raw_low 比较。结构锚点统一使用现有 raw 字段，或缺字段即 unavailable。
4. 日线 `signal_limit_up` 与 `open < high` 不能证明成交可达；历史 depth5 不在 enriched，v1 必须把 reachability 和“全部可交易首板”改为 unavailable/日线价格定义基准。

## P2

5. 明确 forward 端点、benchmark 缺失、唯一事件键、相邻/重叠处理和 OOS purge 20 个市场交易日；重叠样本不得直接使用独立逐笔 bootstrap/Sharpe CI。
6. 明确固定 generation 的市场交易日集合，并对窗口逐日期完整性校验，不能以返回行数代替交易日数。

上述意见已合并进 `plan-v2.md`，未采信未经代码证据支持的替代接口或外部数据源。
