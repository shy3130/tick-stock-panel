# ISSUE-38 设计评审 v1

结论：**Changes requested**。评审针对 [plan-v1.md](plan-v1.md)；主会话已核对 `daily_market_research.py:362-442`，确认 reader 不会生成 `suspended/buyable/sellable`，blocker 成立。

## Blocker

1. **可达性门会导致恒 unavailable**：plan-v1 把 `suspended/buyable/sellable` 当作整单 required facts，但现有 PIT reader 明确从不生成它们。应改为从 pinned raw OHLC、published bands 与 row presence 派生：缺 row/raw/band 才 unavailable/censor；one-price upper/lower 决定 entry/exit reachability。

## Major

1. **F1/F2 selection 时钟错位**：父事件早于命中确认；父池与命中池若从各自确认日执行，收益路径不可比。必须为父事件冻结同一 `selection_decision_date`，三组从同一下一市场日进入同一 horizon。
2. **父池包含命中池**：selection verdict 不能用 qualified 对含 qualified 的 parent pool。应比较互斥 `qualified` 与 `not_selected`；parent 只做描述。
3. **holding 观察终点不一致**：动态臂提前退出会机械改善 MAE。必须使用共同 20 日时钟，退出后现金价值保持不变到终点，再比较 terminal return/MAE/MFE；实际持有天数单列。
4. **PIT universe 三态未冻结**：artifact/快照完整性未知＝整单 unavailable；snapshot 明确不含 symbol＝`pit_universe_ineligible` denominator audit；后续 bar/horizon 缺失＝event censor。

## Minor

1. **F4 五日诊断分母**：强弱/假突破需要独立 `diagnostic_complete_5d` 分母；窗口不足为 diagnostic censor，不得归入 `unclassified` 或影响 signal/verdict。

## 处理决定

全部接受。plan-v2 将按上述最小修正收敛，并补足 F1/F2 decision-window 中未命中事件的唯一判定时点。
