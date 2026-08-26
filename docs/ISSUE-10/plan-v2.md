# 方案 v2（最终定稿）

当前 `FQuantProvider.get_minute` 与 `MinuteExecutionData` 不能作为本因子输入：已有链路只保留 price/volume 并重建 OHLC，且 route 未按运行 pin。因此本 Issue 在真实 reader 落地前只允许交付 fail-closed 能力检查和接口契约，不得从现有分钟接口构造伪造线段。

生产研究必须注入 run-level immutable catalog manifest reader，固定每个日期的 root/generation/logical/file，并返回原始交易所时间戳或 minute_index、独立 OHLCV、seal 状态和 session。若使用 minute_index，契约固定：每个 session 从 09:30 的区间起点索引 0 开始，bar 的 `close_time` 为起点+1 分钟；午休后 session 独立重置；5m/15m 桶按 close_time 对齐到 09:35/09:45… 与 09:45/10:00…，禁止跨 session。reader 按 09:30–11:30、13:00–15:00 精确检查 bar 集合、桶完整性和缺 bar；聚合/映射规则随 run 输出。

## 冻结基线

`mtf_direction_15m5m_v1` 独立服务：1m 原始 sealed bar 聚合 5m，再聚合 15m；分型左右各 2 根、线段最少 3 根；健康斜率为 ATR14 归一化斜率 0.5–2.0；相邻斜率变化 20% 定义加速/趋缓；距最近已确认分型 ≤6 根。状态只在右侧确认 15m bar 的 `close_time` 生效。5m 只能消费 `close_time <= signal_close` 的完整 sealed bars，检查同向收盘、回调深度 ≤1.5 ATR 和线段未破坏；不得读取 signal_close 之后的 5m bars。

方向标签分别定义为两个 horizon：`return_h = raw_close[label_bar_h]/raw_close[signal_bar] - 1`，h 为 1、2，label bar 是 signal 后第 h 根完整 15m bar；`direction=up` 若 return>0，`down` 若 return<0，`flat` 若等于 0，缺 bar/raw_close 则 censored。样本记录 `signal_close`、`label_end`、horizon、label bar ids 与 label value。按同一 symbol 的 `[signal_close,label_end]` 区间相交去重；OOS split 删除所有跨界区间，不以 session 名称替代区间处理。

## 输出/评估

输出 factor/parameter/code 版本、reader generation、manifest 字节 hash、route metadata、聚合规则、信号确认时间、coverage、删失原因和方向结果。不能复用成交回测的 fallback 或 fill_reachability 标志；不定义止损后修复测试。与无条件方向概率、简单动量/均线基线比较；重叠样本不使用独立 bootstrap。有限训练/验证调参，最终 holdout 不选参；无稳定 OOS 增量即 `rejected`。不接入 short_pool/Agent，不输出交易建议。

## 测试

覆盖真实 OHLC 与重建 OHLC 拒绝、缺 timestamp/minute_index、route generation 漂移、session/午休边界、缺桶、包含/健康/加速/趋缓、确认延迟、长上影、5m 同向/反向、signal 后 bar 禁止读取、跨 session label overlap、raw 缺失和空数据。当前生产 reader 不存在时，API 必须稳定返回 unavailable。
