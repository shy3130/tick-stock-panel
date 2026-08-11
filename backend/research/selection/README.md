# Selection Logic v1

这里实现“为什么选、为什么不选”的可审计选股层，不新增前端。

## 当前边界

- 可执行策略：`app/strategy/builtin/quality_momentum_v1.py`。
- 研究入口：`python -m research.selection.run_selection_logic_v1`。
- 股票池：每个窗口结束日仍上市的全部非 ST 股票，不抽样。
- 正向评分：趋势质量 32%、动量质量 28%、量价确认 12%、流动性 8%。
- 风险扣分：过热 8%、波动 5%、近期回撤 3%、跳空 2%、极端动量 2%。
- 硬门槛要求收盘价不低于 MA20，最终开仓信号显式排除同日退出信号。
- 行业：当前 `tushare_stock_basic` 不是 point-in-time 分类，只用于最新候选每行业最多
  2 只的展示约束，不进入历史回放。
- 消息：接口要求带时区 `published_at`；本地没有历史消息库时保持关闭，不伪造中性以外信号。

## 早盘集合竞价叠加

`python -m research.selection.run_auction_selection_v1 --date YYYYMMDD` 调用 Tushare
`stk_auction`，把 9:26 后的当日竞价事实叠加到前一完整交易日的质量动量候选。原始快照
写入 `data/tushare_auction/`，完整候选审计写入 `artifacts/archive/selection/`。

v1 不用当日竞价重新拟合基础评分，只做固定确认/风险过滤：竞价涨跌幅 -1%~+3%、
成交额至少 100 万元、量比至少 0.5 才确认；低开超过 2%、高开超过 5% 或量比超过
5 直接拒绝，其余进入观察。规则尚未通过历史 OOS，只能生成早盘研究候选，不能表述
为最终买入建议。未收盘行情不得写入日线或 enriched 分区。

## 一进二竞价 v1

`python -m research.selection.run_first_board_second_day_v1 --date YYYYMMDD` 是独立的
specialized auction runner，不混入标准日线引擎。前一完整交易日
`consecutive_limit_ups == 1` 定义首板且此前非连板，ST、*ST、退市风险和非正常上市股票
显式拒绝。次日 9:25 的竞价成交额除以首板日全天成交额：8%~12% 得 30 分，12%~20%
线性降至 0 分，低于 8% 或超过 20% 淘汰；竞价涨幅 6%~8% 得 30 分且为硬门槛；
MA5>MA10>MA20 且三条均线都高于前一日得 20 分且为硬门槛；首板阳线从开盘下方穿到
收盘上方并跨越 MA5/10/20/60 中至少两条，额外得 20 分。

2026-08-10 实跑的前日首板池为 62 只，竞价全部匹配，严格规则选中 0 只。最接近的
海正药业占比 10.39%、多头且一阳穿三线，但竞价涨幅 5.80% 未达到 6% 下限，因此仍淘汰。
策略不会自动放宽阈值凑票。专用产物为
`artifacts/archive/selection/first_board_second_day_YYYYMMDD.json/csv`；只有本地保存的历史
竞价日期才能诚实复盘，9:25 买价仍是假设成交，队列位置和真实成交不可验证。

产物：

- `artifacts/archive/selection/selection_logic_v1.json`
- `artifacts/archive/selection/selection_logic_v1_latest_audit.csv`
- `artifacts/archive/selection/auction_selection_YYYYMMDD.json/csv`
- `artifacts/archive/selection/first_board_second_day_YYYYMMDD.json/csv`

所有四个窗口都已在本实验前可见，因此结果只属于历史诊断。策略在两个 2025 强势窗口
改善明显，在 2026 目标窗口退化，后目标窗口仍显著亏损；当前生命周期为
`experimental`，不得替换生产默认策略。

## Selection MVP v2 (P15)

`python -m research.selection.run_selection_mvp_v2` 是当前选股 MVP 的统一历史验证入口。
它不调用前端，也不改生产默认策略：每日先按上市/退市日期、有效行情和当前非 ST 状态
代理构建动态全市场股票池，再执行已有 `quality_momentum_v1`，按截面百分位取
Top 5/10/20。信号在收盘 `t` 形成，统一假设下一交易日开盘买入，并按 1/3/5/10 个
交易日后的开盘生成标签；未来不可成交的入选项保持缺失，不允许用下一名替补。

可选因子不是独立策略。固定候选仅有两种：基础质量动量，以及 80% 基础百分位加 20%
现有 `custom_factor` 百分位。120 日训练、40 日测试、40 日步长；每折只用训练段的
“5 日 Top10 平均超额 - 0.25 × 最差相位回撤绝对值”决定是否叠加因子。测试段不参与
公式、权重、K、持有期、阈值或 early stopping。

截至本地 2026-08-10 数据，8 折测试共 320 个信号日。基础版 5 日 Top10 平均净收益
-0.065%，组合胜率 51.56%，个股胜率 44.54%；因子叠加为 -0.205%、49.69%、
44.90%。训练折投票 4:4，按基础版平局优先规则关闭因子。训练选择后的结构牛市组合
胜率 55.28%、平均净收益 +0.286%，但相对动态可交易等权池平均超额 -0.260%；结构熊
胜率 50.25%、平均净收益 -0.413%。这不支持生产晋级，也不支持“牛市选股已有稳定
alpha”的说法。

本地没有历史 ST/更名区间，只能把当前非 ST 状态作为代理；这会引入 point-in-time
偏差，报告已显式记录。下一阶段不是继续扫权重，而是先补历史证券状态与企业行为数据，
冻结 v2 后积累全新日期，再做真正 OOS 观察。

产物：

- `artifacts/archive/selection/selection_mvp_v2.json`
- `artifacts/archive/selection/selection_mvp_v2_latest_audit.csv`
- `artifacts/archive/selection/selection_mvp_v2_daily_top20.csv`
