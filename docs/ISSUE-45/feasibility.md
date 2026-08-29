# ISSUE-45 可行性评估

## 结论

有条件可行。现有 generation-pinned sealed canonical reader 与 `hold_firm_patterns.adapters.PinnedCanonicalDailyReader` 足以承载纯日频事件研究。独孤趋势仅消费单标的有序日 K 前缀，不接外部行情、默认池或交易执行。

## 复用边界

- 读取：复用 `PinnedCanonicalDailyReader` 与 canonical identity。
- 结构：复用 `Bar`、严格 Pydantic envelope、显式 censor、固定 OOS/verdict/provenance 模式。
- 禁止：复制生产 reader、全局交易日填充、把日线结果表述为分钟级 MERA、写入 `data/`。

## 数据缺口

MA200 warmup、缺棒和 horizon 不足必须进入 censor ledger；OOS 样本低于预注册门槛返回 verdict `unavailable`，不改写为 rejected 或 accepted。
