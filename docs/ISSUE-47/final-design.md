# Issue #47 冻结方案

## 定义版本

`pre_surge_features_v1`

## 参数

|因子|冻结定义|
|---|---|
|F1|最近 10 个交易日存在 PIT 涨停收盘|
|F2|向上缺口至少 2%，缺口日起至第三个完整交易日最低价均未回补前收盘|
|F3|标的连续上涨收盘至少 2 日且严格强于同日基准连续上涨长度|
|F4|最近 5 日每日日量均高于此前 20 日平均量|
|组合|F1–F4 均可评估且至少 3 项命中|

## 时序

F1/F3/F4 收盘确认；F2 在 t+3 收盘确认。每个 evidence 输出 `available_date`、窗口、原始值与删失原因。任一 detector 对相同历史前缀的输出不得受后续数据影响。

## 研究统计

future-surge 标签仅由评估器在后验窗口生成。单因子与组合分别输出必要率、充分率、无条件率、lift、置信下界和 verdict。最终测试集不得参与参数选择。

## 架构

实现位于 `app.services.daily_event_research.pre_surge`，消费 shared `Detection` 契约；不做文件 I/O，不写 `data/`，不接 short_pool、交易生命周期或外部行情。

生产编排绑定 canonical、PIT market facts 与 presence universe 三份 pinned identity；API 为 `POST /api/research/factors/pre-surge-features/evaluate`。
