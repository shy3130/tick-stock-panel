# Issue #50 Design Review 1

## 首轮检查

1. V1 与 V3 的证据链不足，不能用价格走势冒充定义或公告；决定固定为 unavailable，并让聚合保留独立 capability/verdict。
2. V2 缺少同日 PIT 事实时若按 inactive 会虚增分母；修正为显式 `censored`，原因码 `censor_pit_fact_missing`。
3. V4/V5 可能引入未来数据；冻结为截至当日的 MA、60 日窗口及不含当日的前 20 日窗口。
4. 只报告命中下跌会产生单边叙事；增加错过反弹与规避下跌的对称统计。

## 首轮决定
检测器必须纯内存、无网络/文件写入；组合只做研究统计，`promoted=False`，不连接交易路径。
