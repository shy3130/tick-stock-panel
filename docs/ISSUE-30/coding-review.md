# ISSUE-30 coding review 修复记录

关联：[Issue #30](https://github.com/wf2311/fm-workbench/issues/30) · [final-design.md](final-design.md)  
审阅输入：`agent://ReviewAnchorCode`，基线 `7bf2982`。初始结论：**Reject（9 项均需修复，confidence 0.99）**。本文件记录逐项核实与修复；代码实现仍待主会话统一验证。

## 九项 findings 与处置

| 编号 | 优先级/置信度 | 发现（核实摘要） | 修复与测试 |
|---|---|---|---|
| R1 | P1/0.99 | `_FactsView.band` 在 markets `pre_close` 缺失时回退 canonical 上一交易日 raw close，违反 markets PIT 事实缺失必须 unavailable。 | 删除 `_previous_raw_close` 与全部调用；`pre_close` 缺失直接 `limit_band_facts_incomplete`。服务测试验证 canonical 有前收也不回退。raw pre_close 由共享 pinned reader 提供。 |
| R2 | P1/0.98 | 单候选卖出持续阻塞时没有 TradeRecord，原实现把 `blocked_exit_days` 留为 0，低报执行事实。 | 终态 ledger 用单候选 `execution.sell_suspended + execution.sell_limit_down` 计数；保留首个实际 terminal reason。服务单测覆盖 3+2=5。 |
| R3 | P1/0.99 | 正常过滤为 false 的 original/inverted/random 臂被跳过，无唯一 ledger 行，`n_filtered` 与 layers 丢失。 | 每个 candidate×arm 都写一行：`not_retained/arm_filtered` 或 traded/blocked/censored；virtual outcome 统一从 none ledger one-to-one join。服务端到端测试覆盖。 |
| R4 | P2/0.97 | markets generation 打开失败抛 FileNotFoundError/ValueError，被 API 误映射为 503。 | `_resolve_markets_pin` 捕获 OSError/RuntimeError/TypeError/ValueError，转 `UnavailableError(markets_generation_unopenable)`；API 返回研究 `status=unavailable`。服务/API 夹具覆盖。 |
| R5 | P2/0.98 | capability 只检查 canonical，未验证 markets pin/reader openability/字段能力。 | capability 无条件返回 `markets_facts{pin,opened,provides,reasons,detail}`，调用同一 `_resolve_markets_pin`；缺 pin/open 失败均 `available=false`。API capability 测试覆盖。 |
| R6 | P1/0.99 | `n_retained` 把 precheck 成功和 filter decision 混淆，保留后被阻塞/删失的事件被漏计。 | `n_retained` 只统计 `filter_retained is True`；`n_candidates_executed` 独立统计实际 engine 调用。服务单测覆盖 retained-but-blocked。 |
| R7 | P1/0.98 | 无 MA 金叉时跳过 markets pin，可能返回未验证来源的 `status=ok`。 | evaluate 在信号计算后无条件 `_resolve_markets_pin`，无信号也填 markets provenance；服务单测覆盖缺 pin 与空信号成功路径。 |
| R8 | P1/0.95 | 任意 raw OHLC 非法导致整只 symbol 丢弃，T+1 非法 raw_open 无 candidate censor。 | raw 字段保留为 None；T+1 `raw_open=None` → `censored:invalid_open`，symbol/信号全集保留；T+1 外的 raw high/low 缺失在 panel flags 阶段整单 unavailable。服务测试覆盖。 |
| R9 | P1/0.94 | 无近 20 日阳线时 none 也被删失，基准样本发生选择偏差。 | anchor None 不再短路；none 继续执行 T+1/horizon 预检和 engine；original/inverted/random 写 `filter_retained=None` 的 `anchor_unavailable` ledger 行。服务测试断言 none 调用一次、三臂均不可用。 |

## 共享 reader 依赖

#29 owner 已在其 worktree 落地并确认公共契约：

```python
load_pinned_market_facts(
    canonical_manifest,
    symbols,
    market_days,
) -> PinnedMarketFacts
```

返回 `generation`、`manifest_sha256`、`rows[(symbol,date)] -> MarketFact`；`MarketFact` 含 raw OHLC、`pre_close`、`published_limit_up`、`regime`、`is_st`、`name`、停牌/可买卖字段。#30 worktree 已逐字同步该 loader/types/from_canonical_manifest 及 repository 的 `generation_pinned_market_facts_reader` 注入。服务不再从 canonical fallback `pre_close`；共享 reader 无 raw pre_close 时按 R1 fail-closed。

## 验证边界

- 已完成源码级修复、测试用例扩充、两个源文件语法检查；按主会话要求未运行 pytest、lint、format、build，也未提交。
- 主会话合并共享 reader 与本分支变更后，应运行新增服务/API 测试及全量回归，重点关注 shared loader 实际 raw `pre_close` 数据覆盖和 API import 路径。

## 二次 review（P1）处置
- **A：shared reader 过期/事实为空**：已两次从 #29 owner worktree 逐字同步最新 `daily_market_research.py`（第二次在 owner 正式修复后覆盖 #30 本地临时 `.capitalize()` 行），主会话最终修复后第三次 `cmp` 复核 reader 与 repository property 区段仍字节一致。最新版含 `MarketFact.pre_close`、raw OHLC、`published_limit_up/down`、`regime`、`zrspj` 显式 payload 映射、不完整行整体跳过的 fact gate、pin hash 与 `close()` 生命周期。repository property 区域与 #29 diff 为空，无需改动。
- **B：capability 生命周期/字段门禁**：`_open_markets_reader` 支持 generation 字符串或 mapping pin；capability 通过 reader 的实际 schema 元数据抽查 raw/pre_close/ztj/regime，缺任一项即 `markets_fact_fields_unsupported`，并在 `finally` 关闭 reader。`_resolve_markets_pin` 同样 finally-close，identity hash 校验保持 fail-closed。
- **Integration test**：`tests/data_providers/test_daily_market_research.py::test_canonical_pinned_loader_reads_raw_preclose_and_closes` 使用真实 DuckDB 文件、真实 manifest、真实 `load_pinned_market_facts`，断言 raw/pre_close/upper/lower/regime；不再只依赖 monkeypatch bundle。


## 最终 pin identity 同步

- #29 完成 `pin_identity_verified`/`generation_manifest_match` 修复后，#30 再次逐字同步 reader；repository property 同步复核无差异。
- capability 顶层现在要求 `pin_identity_verified() is True`，否则返回 `available=false`、`markets_pin_identity_unverified`，并透出 verification mode；reader 在 capability 与 `_resolve_markets_pin` 两路径均通过 `finally` 关闭。

## 严格 hash pin 最终契约

- 已同步 #29 最新严格版 reader：legacy string pin 的 `pin_identity_verified()` 为 false；mapping pin 必须同时提供 `generation` 与 `manifest_sha256`，否则 fail-closed。#30 capability/evaluate 均强制 identity gate。
- #30 不复制 `canonical_history` publisher；PR 明确依赖先合并 #29，再将 #29 base 集成到 #30。

## raw_close 口径最终同步

- #29 最新 reader 已将 `raw_close` 固定取 daily_markets 的 `price/Price`（当日收盘）；`zspj/Zspj` 不再作为 raw_close，`zrspj/Zrspj` 仅作为 `pre_close`。#30 已逐字同步，并以 price 与 pre_close 异值的真实 DuckDB 测试断言该口径。
