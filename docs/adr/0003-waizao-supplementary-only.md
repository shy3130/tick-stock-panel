# 0003 — waizao 第三方源限定为"补充面"，核心行情永不回退第三方

- **状态**：已接受（2026-07-02）
- **相关**：[数据源设计](../superpowers/specs/2026-07-02-strategy-dsl-and-fquant-datasource-design.md) Part B、AGENTS.md 红线 #2

## 背景

`../fquant`(Go) 的 `Manager` 把 waizao(wz) 作为 `Daily` 与 `RealtimeQuote`（**核心行情**）的最后兜底源。waizao 是 **token 鉴权的第三方外部商业行情 API**（`WZAPIBaseURL`+`WZAPIToken`，端点如 `getStockHSADayKLine`），性质上不同于 engine-data / fstore / tdx——后三者是自有局域网本地设施。

两处冲突：
1. **红线冲突**：panel 的 AGENTS.md 红线 #2 明令"不要直接连接外部行情接口（Tencent/新浪/第三方）"。让第三方 API 兜底核心行情违反其本意。
2. **口径漂移**：waizao 的 `fq=1` 前复权口径与 engine-data 复权口径不同，作为核心日 K 的 fallback 会静默混入不一致口径。

考虑过：
- **W2** 保留 waizao 全量兜底（含核心行情）+ 修订红线 + 自觉接受口径差异。
- **W3** 完全不接 waizao（放弃梯队/情绪面）。

## 决策

**W1**：waizao 仅作**补充面**数据源——涨停梯队（MarketLeaders）、情绪面（MarketEmotion）、板块（Boards）、名称（StockName）。
**核心日 K（Daily）与实时（Realtime）的源链中不包含 waizao**；核心行情只走本地三源：日 K = engine-data → fstore fallback；实时 = tdx。

## 后果

- ✅ 红线 #2 对"核心行情"的意图完整保留（核心行情不碰第三方）。
- ✅ 规避核心日 K 的复权口径漂移。
- ✅ 仍获得梯队/情绪面/板块/名称能力。
- ⚠️ 红线 #2 需补一条**解释性注记**：补充面数据（梯队/情绪/板块）经 `data_providers` 抽象层内的受控适配器接入第三方是允许的；被禁止的是"核心行情绕过抽象层/回退第三方"。
- ⚠️ waizao 不可用时，只损失补充面能力，核心选股/回测/监控不受影响（能力门控降级）。
