# ISSUE-45 首轮设计复核

## 复核结论

方案保持深而小：detector 只做确定性 T1/T2/T3/M3 判定，evaluation 统一承担窗口、成本、删失、OOS 与 verdict。

## 关键决策

1. 预注册 MA24/72/200 与 MA20/70/200 两个变体。
2. T2 使用 MA 快慢线之间的回调带，支持 fixed 与 ATR 两种已冻结模式。
3. M3 只用 signal day 与 20 个交易日前的收盘计算区间涨幅，并冻结上限 30%。
4. signal day 的数据可用于检测，forward horizon 仅用于结果标签。
5. 研究不足显式 `unavailable`，不自动推广。
