# Issue #48 冻结方案

## 检测契约

所有检测器返回 `daily_event_research.models.Detection`，纯计算检测器不访问网络、文件或用户数据。S2-S7/S10 由 production 在 provider research reader 成功装载后调用。

|信号|冻结口径|available/执行时点|
|---|---|---|
|S1|当日 high > 前 60 根 high 最大值，且当前红柱段截至当日的峰值 < 前一完整红柱段峰值；MACD EMA12/26、DEA9、hist=`2*(DIF-DEA)`|close / 下一交易日 open|
|S2|14:30 后跌幅 >=2%；尾段路径速度 >= 此前绝对路径速度 3 倍；收盘在全日底部 20%|close / 下一交易日 open|
|S3|触 published limit-up 后收盘未封住；记录首次触板和开板次数|首次触板后 / 同日信号分钟价|
|S4|全日量 >= 前 5 日均量 2 倍；绝对涨跌 <=1%；盘中高点 >= 昨收 3%|close / 下一交易日 open|
|S5|触 published limit-down；封死、翘板未翻红、翘板翻红三支|首次触板后 / 封死不可达，其余同日信号分钟价|
|S6|高开 >=2% 后连续 5 分钟收盘低于累计 VWAP|第 5 根确认 / 同日确认分钟价|
|S7|10:00-10:30 未突破开盘 30 分钟高点，且 10:30 收盘低于累计 VWAP|10:30 / 同日 10:30 价|
|S8|最近三根均 `close < open`；doji 打断|close / 下一交易日 open|
|S9|`(上一根原始收盘-当日原始开盘)/上一根原始收盘 >= 0.05`；原始价格缺失时 censor|当日 open / 当日 open|
|S10|股价高于昨收；同分钟累计换手 >= 前 5 个完整日同时点均值 2 倍|首次满足分钟 / 同日信号分钟价|

日线 evidence 保存 ISO `available_date`；盘中 evidence 保存 ISO `available_at`、条件原始值、`existing_position_required=true`、执行 session/price/reachability。S10 的当日和前 5 日 `ltgb` 都必须来自 exact-date `daily_markets`，且 manifest `source_version <= signal minute`；不满足只删失 S10。

## 数据与完整性

`CatalogPinnedEscapeRiskIntradayReader` 在构造时按交易日分别解析 `tdx_minutes` 和 `tdx_trans`，并冻结 catalog route 对应的 generation manifest 身份。历史 `pinned_immutable` 精确日期范围必须先于 later preliminary 回退选择。

每个交易日按全部 symbols 批量查询。分钟表必须包含 0..239 全部索引；canonical bar close 固定为 `09:31..11:30,13:01..15:00`，与逐笔交易桶标签分离，禁止把 09:30 桶标签当成 bar 的 `available_at`。逐笔只允许映射到冻结的 09:25/09:30、上午、下午和 14:59/15:00 桶。分钟与逐笔允许午盘/收盘的少量逐桶归属差异，但全日成交量必须严格守恒。high/low/amount/cumulative VWAP 一律来自逐笔；任何 route/query/coverage/integrity 缺口都进入 symbol/day 删失，不回退 raw 或日线近似。historical name 是 ST/制度判断的必需 PIT 事实：缺失或空值时删失该日，包含 ST 时固定为 `st_5`，且必须先于上下限价推导。

## 聚合

`aggregate_escape_signals` 接收全部 S1-S10 detections、按 symbol 的 bars、`horizons=(1,3,5,10)`、成本、显式基线和可选 benchmark。盘中可达信号使用 evidence 里的同日执行价；收盘确认信号使用下一交易日开盘；N=1 表示执行日收盘。成本为两腿 round-trip。不可达信号计入证据/删失，不伪装为可执行样本。

buy_hold 由 bars 的持有定义得到；MA20、ATR、prev_close 缺失时输出 `unavailable_no_baseline`。未冻结真实 OOS 结构化基线时 verdict=`unavailable_no_frozen_oos_baseline`，不得以样本内均值宣称接受；要求 benchmark 但未提供时为 `unavailable_benchmark_missing`。多信号仅输出计数 bucket，不输出方向指令。

## capability

S1-S10 的代码级 capability 都为 `available`。请求时另报告 intraday reader 的 runtime status、coverage 和逐信号 censor：minutes/trans 不可用影响 S2-S7/S10；PIT 股本不可证明只影响 S10。`require_daily_signal` 与 `minute_approximation=True` 仍 fail-closed。

能力边界通过 `GET /api/research/escape-risk` 暴露；评估入口为 `POST /api/research/factors/escape-risk/evaluate`。
