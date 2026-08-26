# 方案 v2（最终定稿前版本）

## 关键门禁

v1 方案不能安全落地，原因是 canonical generation 未固定、历史涨停制度与 ST 不是 PIT、缺少 `raw_open`、日线无法证明可交易，以及 forward 重叠统计未定义。v2 将这些列为实现前置条件；任何条件不满足，API 返回 `unavailable`，不返回伪造事件或“可交易”结果。

## 固定数据前置能力

新增评估入口必须使用 generation-pinned、排除 local overlay 的 sealed canonical 读路径；若仓库没有该能力则直接 `unavailable: canonical_generation_not_pinned`。请求返回实际 generation、manifest **字节哈希**、代码/参数版本。禁止仅调用合并 overlay 的 `get_enriched_range` 后声称 sealed。

评估前从同一 generation 构造固定市场交易日集合。首板前 60 个完整市场交易日、调整窗口和每一个 forward horizon 预期日期都必须存在有效正 OHLCV；此外，所有实际参与事件结构比较的日期必须有 `raw_close > 0`，首板/调整/突破日期必须有 `raw_high > 0` 与 `raw_low > 0`。缺任何日期/字段，事件或对应 horizon 标记 `censored`，不能以股票行数代替交易日数；不得以复权列回退 raw 列。

## 冻结基线参数

`NShapeParams` 为 Pydantic `extra=forbid`，版本 `n_shape_golden_phoenix_v1`：

- 低位：信号前可见 60 日 `price_position_60d <= 0.35`；
- 首板观察：目标日达到**日期有效、PIT 板块制度与 PIT ST 标记共同证明的价格涨停**，此前 60 个完整市场交易日均非价格涨停；无法证明制度或 ST 状态则事件 `unavailable`；不把现有可能使用当前名称的 `signal_limit_up` 直接当历史首板真值；
- 一字板：已有 OHLC 中 `open == high` 仅作形态排除标记，不宣称成交可达；
- 调整窗口：首板后第 2–10 个市场交易日；
- 缩量：调整期平均 `volume / 首板 volume <= 0.70` 且调整期平均 `volume / 首板前 20 日平均 volume <= 0.90`；
- 结构保持：采用单一固定规则“调整期最低 `raw_low >= 首板日 raw_low`”，删除原先被其支配的 `0.92 × raw_low` 第二门槛。由于没有 `raw_open`，v1 不使用前复权 `open` 推导 raw 实体低点；所有结构和收益比较仅使用 raw 字段；
- 均线：调整末日 `close >= ma5` 或 `close >= ma10` 至少一项，缺指标不可用；
- 二次启动分为 `volume_breakout`（收盘 `raw_close` 突破首板日 `raw_high` 且成交量 ≥ 调整期均量×1.5）与 `second_limit_up`（独立的日期有效价格涨停）；不合并统计。

参数只作为冻结基线；后续仅允许训练集/验证集使用 `walk_forward_candidates` 的单参数有限邻域，不得查看最终测试集选参。

## PIT 时间线、基准与重叠

- 所有事件特征只使用事件确认时点及此前同一 generation 数据；二次启动信号日是 event `signal_date`，前向从其后第一个市场交易日计。
- forward 固定为 `raw_close[T+h]/raw_close[T]-1`，其中 h 为 `[1,5,10,20]` 个市场交易日；每个 horizon 缺数据单独 `censored`。
- 相对收益仅在同一 generation 有明确、同步可读取的 benchmark raw close 时计算；benchmark 缺失则为 null + `benchmark_unavailable`，不以 0 代替。
- v1 的基准命名为“同一日线价格定义下的全部首板”，不命名“全部可交易首板”；不得把日线结果解释为可执行收益。
- 唯一键为 `symbol:first_limit_up_date:signal_date:variant`；同一首板多个变体分别输出但分别统计。每个标的相邻事件按冻结规则只保留第一个完整事件，重叠 forward 窗口在描述统计中标记；OOS 切分在评估时 purge 20 个市场交易日。重叠样本禁止直接套用独立逐笔 bootstrap/Sharpe CI，若无聚类稳健实现则只输出描述统计和样本数。

## 可达性与执行边界

历史 depth5 不进入 enriched，现有 repository 没有历史 depth5 公开读取接口；`open < high` 不能证明买入可达；v1 的 `reachability` 固定为 `unavailable` 或 `censored`，并记录 `daily_price_only`。分钟可达性诊断只能在另有带 provenance 的分钟证据时追加，不能把日线 forward 变成执行回测。

## 输出与接口

新增只读 `POST /api/research/factors/n-shape/evaluate`，请求严格限制日期、标的上限和参数版本，后端返回：schema/factor/parameter version、generation/manifest hash/code version、`status`、事件列表、coverage、warnings、summary 和 `rejected`/`unavailable` 原因。事件证据只含日期、价格、量、比率、结构状态和删失原因，禁止交易语义键。前端若接入只展示后端字段，不重算。默认短线池与 Agent 工具注册完全不变。

## 验证与准入

- 夹具覆盖普通/低位首板、历史首板前已有涨停、缩量失败、结构跌破、一字板、二次放量、二次涨停、停牌/缺日期、制度/ST 不可证明、raw 字段缺失和 benchmark 缺失。
- API 验证 extra 字段拒绝、generation 未固定 fail-closed、raw 字段口径、一字板 reachability、交易语义证据禁令和 horizon 删失。
- 使用真实 published generation 仅做只读研究，记录 generation/manifest hash；不写 `data/`。
- 训练/验证有限调整后严格 walk-forward/OOS；没有稳定成本后增量或样本不足即 `rejected`，不会进入默认候选池。
