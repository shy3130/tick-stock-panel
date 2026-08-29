# ISSUE-38：四类持有形态独立研究

- GitHub Issue：[wf2311/fm-workbench#38](https://github.com/wf2311/fm-workbench/issues/38)
- 分支：`issue-38-four-pattern-hold`
- 基线：`workbench/feature/fstore-engine-duckdb-source@4c94f68`
- 状态：实现、presence_v1 依赖、代码复核与真实 OOS 均完成；四因子因独立样本门槛不足均为 `unavailable`，待依赖/收口 PR 合并后关闭

## 范围

把“首阴持有、横盘突破回踩、低位缓坡、底部平台突破”实现为四个独立、可审计的日频研究因子。四者只共享 generation-pinned 数据读取、PIT 市场制度、执行与统计基础设施；信号、分母、基准、IS/OOS 统计和 verdict 必须独立。

本轮不接前端、默认短线池、Agent、监控、optimizer 或真实交易，不写 `data/`，不输出买卖建议。

## 验收摘要

1. 原稿歧义在实现前冻结，所有自定参数和偏离均可追溯。
2. 信号可按任意历史日期截断复算，无未来函数。
3. canonical 与 markets generation/manifest 固定，必需事实缺失 fail-closed。
4. 四因子独立输出事件、对照、删失、IS/OOS、成本后指标、失败分层和 verdict。
5. T+1、停复牌、涨跌停阻塞、跳空、费用滑点和不可成交样本进入真实分母。
6. focused/full tests、Ruff F/E9 与独立 coding review 完成。
7. 真实 production evaluation `status=ok`、无 universe 阻断；四因子逐项输出 `unavailable`，没有整体打包通过或生产提升。

## 文档清单

- [可行性评估](feasibility.md)
- [首版方案](plan-v1.md)
- [首轮独立评审](review-v1.md)
- [调整方案](plan-v2.md)
- [二次独立评审](review-v2.md)
- [冻结设计](final-design.md)
- [实现代码复核](coding-review.md)
- [验证与验收记录](verification.md)
- [真实 OOS 机器摘要](oos-verdict.json)

## 真实 OOS 收口（2026-08-29）

- canonical generation `20260829T002957-4b1bfcad`，manifest SHA-256 `0d5b5a457e7fa8c25bb047005b20cc6ca06ed19092f7ce20ba65f4604dfdd372`。
- markets generation `20260829T000704`，manifest SHA-256 `a2a9d2b8208af33f4bcb66bcbe46a02ee836659c337deab4d0fd550ffead22a8`。
- universe presence generation `20260829T020332Z-6e648967c37e6739`，manifest SHA-256 `2c407072371bc024de46fd2d5b1d282d964b478e94196da9fc575a0bac1781d4`，100 个精确日期 identity。
- 10 个确定性排序标的的生产 evaluation 返回 `status=ok`；`first_yin_complement`、`breakout_pullback`、`low_gentle_slope`、`bottom_platform_breakout` 分别因各自 OOS 有效样本不足得到 `unavailable`。
- 这解决了历史 PIT universe 的工程阻断，但不把样本不足改写成因子有效；没有因子进入默认短线池、Agent 排序或真实交易。
