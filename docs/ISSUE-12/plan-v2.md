# 方案 v2（终审修订）

当前生产链路不满足弱转强研究，故实现前必须 fail-closed。run-level immutable manifest 必须一次性固定 canonical 日线 generation、T/T+1 分钟/集合竞价 route、以及实际使用的逐笔/盘口 route、各自 generation/文件校验和/覆盖范围；还必须固定 PIT 制度、ST、流通股本输入的 generation、校验和、覆盖及每条记录的 `available_at`。每个 PIT 记录不仅要 `effective_at <= signal_time`，还要 `available_at <= signal_time`；晚发布、回填不可区分或缺失即 unavailable。

现有 `get_minute` 重建 OHLC、当前 `signal_limit_up`、当前 instruments 和无时点换手率均不满足，不得调用。分钟事件变体按证据门槛独立：只有有可排序逐笔（同秒稳定序号）才可判定触板/炸板/回封顺序；“封板”还必须有对应时点历史盘口/封单证据；否则只能输出 `bar_touched` 或该变体 unavailable/censored，不能把 OHLCV/逐笔成交当封单事实。集合竞价高开至涨停必须有 auction evidence，否则 censored_preopen；signal_time 是最后确认原始事件结束时间，forward/执行诊断严格从后一完整 bar 开始。

人气核心没有 PIT 快照时 `core_status=unavailable`，不得使用事后题材映射。放量 v1 只允许 raw volume/amount 历史滚动基线；换手率需 PIT 股本。所有输出包含证据、删失、manifest、版本和 observed_at，不含交易语义，不接 short_pool/Agent。对照组、OOS 区间 purge、成本和可达性必须按冻结事件时间处理；无稳定增量即 rejected。

测试：缺 manifest/逐笔/盘口/PIT available_at、重建 OHLC、集合竞价、同窗成交、所有事件变体门槛、核心 unavailable、停复牌和缺数据均 fail-closed。
