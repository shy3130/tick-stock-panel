# 方案 v2 Review

二审结论：**有两项必须修订，修订后可进入最终定稿和编码。**

1. P1：逐日完整性门禁必须同时检查实际使用的 `raw_close`，以及首板/调整/突破日的 `raw_high`、`raw_low`；缺失时不得以复权列回退。
2. P2：`raw_low >= 首板 raw_low` 已经支配 `raw_low >= 首板 raw_low * 0.92`，必须删除第二个门槛，保留单一“不跌破首板 raw_low”规则。

二审同时确认：generation-pinned sealed reader、带 manifest 字节哈希的 provenance、PIT 制度/ST 前置、日线不可达性降级、benchmark/forward/重叠及 Agent 隔离边界已明确；即使现有代码尚无这些读取能力，实现也必须 fail-closed，而不能伪造能力。上述两项已合并进当前 `plan-v2.md`，现在可形成 final-design。
