# 方案 v1 Review

不通过。必须要求真实 timestamped OHLCV/逐笔或盘口 provenance 的 immutable minute reader；现有 `get_minute` 重建 OHLC 不满足。日线涨停、ST、板块制度与换手率必须是 effective_at<=signal 的 PIT 输入；不能复用 current instruments/signal_limit_up/最新流通股本。人气核心无 PIT 时只能 unavailable，删除未定义核心代理。可达性不能由 bar touch 推导；集合竞价高开至涨停需单独 auction evidence。信号时间必须是最后确认原始事件的 observed_at，收益/成交从严格后续 bar 开始。运行开始需固定跨源 snapshot manifest 与 route 校验值。