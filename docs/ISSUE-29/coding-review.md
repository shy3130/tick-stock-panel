# ISSUE-29 coding review 修复记录

> ReviewZuoyiCode 共 20 项；实现证据与最终 identity 规则如下。测试由主会话执行。

| # | Finding | Evidence / resolution |
|---|---|---|
| 1 | reader 字段不一致 | `daily_market_research.py` MarketFact 使用 published_limit_up/down |
| 2 | 成本未扣除 | `_seg` 传 cost_bps 并扣双边成本 |
| 3 | holding 阻挡错误 | blocked_until 只对实际/待完成 horizon 生效 |
| 4 | ATR run max stale | ATR line 按 entry→当前 rolling max |
| 5 | upper 被误作不可卖 | `_sellable` 仅 signal_limit_down + raw 同价 |
| 6 | markets coverage 半运行 | 缺 pinned facts 整单 UNAVAILABLE_MARKETS_PIN |
| 7 | T+1/horizon 行偏移 | service 使用 reader.market_days 与 row_by_date |
| 8 | reader 泄漏 | API evaluate/capability finally close |
| 9 | pin identity 未校验 | canonical generation/manifest/hash 校验 |
| 10 | diagnostics 分母零 | denominator audit 接真实 diagnostics |
| 11 | 卖飞 peak 含历史 | evidence entry_date，peak 限定 entry→exit |
| 12 | ok + unavailable verdict | OOS 不足返回顶层 unavailable |
| 13 | response_model 缺失 | API 声明 ZuoyiDefenseResponse union |
| 14 | 非法请求 422 | route 手动 Pydantic 校验并映射 400 |
| 15 | symbol 未规范化 | canonical 6 位交易所格式、去重 |
| 16 | MA60 无 warmup | pre-start 59 market days 加载门禁 |
| 17 | 入场日 breach | `_exit_for_line` 从 horizon_idx[1:] 扫描 |
| 18 | paired bootstrap 缺失 | common entry_id diffs、seed42/500 bootstrap |
| 19 | market_days provenance 错误 | provenance 使用 pinned calendar 实际计数 |
| 20 | reader 伪造事实 | raw/pre_close/ztj/regime/name 真实投影，缺失 fail-closed |

## 最终 identity verification 证据

- `canonical_history.py:snapshot_identity` 校验目标快照同目录 `manifest.json` 的 generation、logical entry/file，并计算 manifest SHA-256。
- full canonical publish 将 `source_generations` 固化为 `{generation, manifest_sha256}`；incremental 从本次 calendar snapshot paths 至少刷新 `markets`/`tdx`，legacy parent 其余项按原值继承。
- `daily_market_research.py:from_canonical_manifest` 对 mapping pin 强制 expected hash 且严格匹配；legacy string pin 返回 `pin_identity_verified=False`、`pin_verification_mode=missing_expected_hash`，evaluate/capability fail-closed。
- production probe 证实 direct `zgj/zdj` 与 payload `Jrkpj/Zrspj/Ztj` 并存；reader 保留 direct+payload fallback。
- 已发布 legacy generation 会诚实返回 unavailable；下一次 full/incremental canonical publish 后自动具备可验证 markets pin 能力，不触碰用户 `data/`。
- 终审 raw_close 证据：production daily_markets 的当日收盘来自 direct `price`/payload `Price`；`raw_open/high/low` 仍为 `Jrkpj/Zgj/Zdj`，`pre_close` 仍为 `Zrspj`。reader 的 `_RAW_QUOTES` 已按此映射，真实 DuckDB 测试以 `price=10.4`、`pre_close=9.5` 异值断言防止误配。
