"""Trading 域 — 单笔交易生命周期 + 决策审计。

两条 append-only 事件流是本域的地基:
- trade_events.jsonl    交易事实(开仓/成交/加减仓/平仓)
- decision_audit.jsonl  决策事实(门禁拦截/放行均留痕,永不清理)

单笔文件 trades/{trade_id}.json 是当前事实的缓存投影,
历史以事件流为唯一事实源。
"""
