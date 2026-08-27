# 方案 v1

定义 `weak_to_strong_v1`：T 日 PIT 制度确认的非一字价格涨停，成交量/换手达到冻结倍数；T+1 开盘相对 T 收盘高开，且真实分钟 reader 在 T+1 固定窗口确认首次封板、炸板、回封和可达性。人气核心作为独立维度：无 PIT 数据则 unavailable，不能使用事后涨幅标签。

只读服务/API 使用 generation-pinned sealed daily/minute reader；缺 reader、制度/ST、timestamp、分钟完整性或可达性即 unavailable/censored。输出结构化 evidence、信号时间、删失原因、对照组、成本/前向诊断，不输出交易语义，不接 short_pool/Agent。先基线对照，再有限 walk-forward/OOS；无稳定增量则 rejected。测试覆盖一字板、烂板、低开、高开未封、首次封板、炸板回封、停复牌、核心代理和缺数据。