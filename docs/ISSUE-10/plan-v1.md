# 方案 v1

## 冻结基线

新增独立 `mtf_direction_15m5m_v1` 服务，不进入 `FIELD_REGISTRY`。1m provider 聚合为 5m，再按同一交易日/午休边界聚合 15m；参数固定：分型确认左右各 2 根、线段最少 3 根、健康斜率为 ATR14 归一化斜率绝对值 0.5–2.0、加速/趋缓由相邻斜率变化 20% 定义、距最近确认分型不超过 6 根。状态只在右侧确认 K 线收盘后生效。

5m 确认固定检查同向收盘、回调深度 ≤ 1.5 ATR 和未破坏最近线段；未来方向标签是信号后第 1/2 根完整 15m K 的 raw close 方向。预测与执行分离。

## 数据与失败关闭

通过注入式 generation-pinned minute reader 读取 sealed 1m/5m 数据；没有固定 generation、catalog coverage、连续交易时段、午休边界或有效 OHLCV 即 `unavailable/censored`，不得使用 `get_minute` overlay 或外部 fallback。输出 generation/manifest hash、聚合规则、信号确认时间、coverage、删失原因、方向结果和 `daily_price_only`/分钟可达性状态。

## 评估

与同时段无条件方向概率、简单动量/均线规则比较命中率、校准误差、收益、MFE/MAE；重叠标签按 symbol/session 去重并在 OOS 边界 purge 两根 15m bar。不得把独立逐笔 bootstrap 用于重叠标签。先基线，再只在训练/验证调确认长度、分型阈值、斜率/距离和持有周期，最终 holdout 不选参；不足则 `rejected`。

## 测试

覆盖包含关系、健康上升/下降、加速/趋缓、分型确认延迟、长上影、5m 同向/反向、止损后修复、午休、缺 bar/catalog 与 generation pin 缺失。默认不改 short_pool、Agent、交易事实流或 data/。
