# Issue #50 Design Review 2

## 二轮复核

- V4 的 MA5<MA10<MA20、三条一阶斜率均小于零、close<MA20 必须连续五个可评估交易日；窗口不足删失，不提前触发。
- V5 的 60 日高回撤、前 20 日平台低破位、前 20 日均量两倍三条件采用 AND，不允许单条件降级。
- 组合 active 是启用且 available 类的日内并集；全部参与类删失才使组合日删失。
- verdict 分层为 capability unavailable、样本不足 unavailable、无稳定改善 rejected、稳定净收益改善 accepted；稳定性要求整体及前后半段净收益均为正。
- portfolio 使用显式 forward returns 的等权日期收益；全排除日按现金 0 收益，同时披露总收益、年化收益、Sharpe 与最大回撤增量。

上述边界与 fail-closed 规则通过后冻结进入 final-design。
