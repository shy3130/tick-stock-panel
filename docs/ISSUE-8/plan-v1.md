# 方案 v1

## 目标与边界

新增独立的 `n_shape_factor` 研究服务，不把事件状态塞进 `FIELD_REGISTRY`，不改变现有 `short_momentum_quality_v1`。服务输入 repository、信号日期范围和冻结参数，输出事件结果封套；候选池接入另行以审计结果为门禁，默认不启用。

## 冻结基线参数

`NShapeParams` 使用 `extra=forbid`，版本 `n_shape_golden_phoenix_v1`：

- `low_lookback_days=60`、低位 `price_position_60d <= 0.35`；
- 首板前回看 60 个交易日，首板涨停幅度按 canonical `signal_limit_up`，且 `open < high`（一字板删失）；
- 首板后调整窗口 `[2, 10]` 个交易日；
- 调整期平均成交量 / 首板成交量 `<= 0.70`，同时调整期平均成交量 / 首板前 20 日平均成交量 `<= 0.90`；
- 结构保持：调整期最低 `raw_low >= 首板实体低点`，且 `raw_low >= 首板开盘价 * (1 - 0.08)`；两项均为固定规则，不选择最优支撑；
- 均线状态：调整末日 `close >= ma5` 或 `close >= ma10` 至少一项，缺指标时不可用；
- 二次启动拆成两个变体：`volume_breakout`（收盘突破首板高点且成交量 >= 调整期均量 * 1.5）与 `second_limit_up`（独立涨停事件）。不混合统计。

参数是首轮研究基线，不因历史结果临时修改；后续只允许 `walk_forward_candidates` 的单参数有限邻域。

## PIT 时间线与数据口径

- 每只股票按日期排序；首板识别只使用首板当日及此前行。
- 调整状态在首板后的每个交易日逐日推进；只有满足最短窗口后才可能产生信号。
- 信号日的所有证据来自信号日及此前数据；前向收益从信号日之后的可见交易日计算。
- 使用 `raw_low/raw_close` 做事件锚点与收益；复权列只用于展示/一致性检查，不能和 raw 锚点混合。
- 缺少完整窗口、OHLCV、换手或关键指标时输出 `unavailable`/删失原因；不填 0。
- 停牌/缺交易日连续性、首板后跌停和无法确认成交在 evidence 中记录为 `censored`；日线研究不宣称可执行成交。

## 输出契约

`NShapeResearchResult` 包含：`schema_version`、`factor_id/version`、`status`、`as_of`、`params`、`data_provenance`（generation/snapshot hash/coverage）、`events`、`summary`、`warnings`。

每个事件包含：`symbol`、`first_limit_up_date`、`signal_date`、`variant`、`status`、`evidence`、`miss_reasons`、`coverage`、`reachability`、`forward`。证据只允许结构字段（价格、量、日期、比率、状态），禁止 `buy/sell/target/stop/action` 等交易语义。`forward` 逐个 horizon `[1,5,10,20]` 提供绝对/相对收益；不足 horizon 为 null 并带删失。

## 服务与 API

- 新服务读取 `repo.get_enriched_range`，批量按日期/标的处理，禁止直连 provider 或文件路径。
- 新增只读 `POST /api/research/factors/n-shape/evaluate`，请求模型严格限制日期范围、标的上限和参数版本；返回上述封套。
- 不修改 `/api/agent` 工具注册，不将该因子接入 Agent 默认短线池；后续只有 OOS 达标才可另开 Issue 讨论启用。
- 前端本 Issue 只在已有 Research 页面增加结果展示（若实际契约需要），不计算任何指标。

## 验证

- 单元夹具覆盖：普通/低位首板、首板前已有涨停、缩量/放量回调、结构跌破、一字板、二次放量突破、二次涨停、停牌/缺数据。
- API 测试验证 extra 字段拒绝、数据不足 fail-closed、证据禁止交易词、forward horizon 删失。
- 使用真实 published canonical generation 做一次只读事件研究，记录 generation 和命中/删失统计；不将本次输出写进 `data/`。
- OOS 结果与“全部可交易首板”基准对照；样本或增量不足时状态为 `rejected`。
